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
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from importlib.resources import files
from itertools import pairwise
from pathlib import Path
from time import sleep
from time import time as wall_clock
from typing import Any
from uuid import uuid4

from trading_research_agents import __version__
from trading_research_agents.agents import Outcome, RunConfig, propose
from trading_research_agents.agents.briefs import BriefError
from trading_research_agents.agents.context import (
    TEXT_CLASSES,
    ContextError,
    declared_in,
    inspect_shelf,
    roster_for,
)
from trading_research_agents.agents.cycle import CycleError, budget_spent
from trading_research_agents.agents.pipeline import read_refusals
from trading_research_agents.agents.session import SessionError
from trading_research_agents.chain import (
    EraLeakageError,
    NullMode,
    ValidationError,
    check_requirements,
    cost_floor,
    read_trades,
    validate,
)
from trading_research_agents.chain.preregistration import PreregistrationError
from trading_research_agents.chain.preregistration import from_record as prereg_from_record
from trading_research_agents.chain.requirements import to_entry as requirements_entry
from trading_research_agents.chain.validation import to_entry as validation_entry
from trading_research_agents.chain.validation import to_record as validation_record
from trading_research_agents.cli import console, shutdown
from trading_research_agents.cli.console import HISTORY_FILE
from trading_research_agents.data import (
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
from trading_research_agents.data.fetch import FetchError, fetch, fetch_checksum
from trading_research_agents.data.parquet import ParquetError, write_trades
from trading_research_agents.data.sources import (
    ChainExhaustedError,
    DataConfigError,
    SourceError,
)
from trading_research_agents.data.sources.registry import UnknownSourceError
from trading_research_agents.envelope import EnvelopeError, load_envelope
from trading_research_agents.execution import FundingSchedule
from trading_research_agents.execution.pretrade import PreTradeError
from trading_research_agents.ledger import Ledger, LedgerError, Verdict
from trading_research_agents.ledger.store import to_record as ledger_to_record
from trading_research_agents.llm import Completer
from trading_research_agents.llm.chain import CompleterChain, Link, parse_link
from trading_research_agents.llm.completer import ProviderError
from trading_research_agents.llm.providers import (
    CallOptions,
    UnknownProviderError,
    provider,
    providers,
)
from trading_research_agents.llm.providers.anthropic import DEFAULT_MODEL
from trading_research_agents.records import write_json
from trading_research_agents.strategy import ExitError
from trading_research_agents.trading import (
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
from trading_research_agents.trading import run as run_trading
from trading_research_agents.trading.canary import CanaryError, CanaryGuard
from trading_research_agents.trading.config import load_funding
from trading_research_agents.trading.feed import FeedError
from trading_research_agents.trading.feeds import UnknownFeedError, live_feed
from trading_research_agents.trading.venues import (
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
    parser.add_argument(
        "--plain", action="store_true", help="plain text even at a terminal (or TRA_PLAIN=1)"
    )
    parser.add_argument(
        "-c",
        dest="once",
        metavar="COMMAND",
        help="run one command and leave, instead of opening the console",
    )
    parser.add_argument(
        "--no-history",
        dest="history",
        action="store_false",
        help=f"do not keep {HISTORY_FILE} beside the project",
    )
    top = parser.add_subparsers(dest="group", required=False)

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

    get_cmd = data.add_parser(
        "get", help="fetch, audit, Parquet and funding for one day, into data/"
    )
    get_cmd.add_argument("instrument")
    get_cmd.add_argument("when", help="YYYY-MM-DD for daily venues, YYYY-MM for monthly ones")
    get_cmd.add_argument("--venue", choices=venues, help="defaults to the venue in strategy.toml")
    get_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    get_cmd.add_argument("--out", type=Path, default=Path("data"))
    get_cmd.add_argument(
        "--max-gap", type=parse_duration, default=None, help="default: [data].max_gap or 5m"
    )
    get_cmd.add_argument("--json", action="store_true")

    pull_cmd = data.add_parser(
        "pull", help="one snapshot of live data through the chains in data.toml"
    )
    pull_cmd.add_argument("instrument", nargs="?", default=None, help="default: [run].instrument")
    pull_cmd.add_argument("--data-config", type=Path, default=Path("data.toml"))
    pull_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    pull_cmd.add_argument(
        "--kind",
        action="append",
        default=None,
        help="only this kind of data; repeat for several. Default: every kind declared",
    )
    pull_cmd.add_argument(
        "--out", type=Path, default=None, help="default: runs/cycles/<cycle>/snapshot.json"
    )
    pull_cmd.add_argument("--env-file", type=Path)
    pull_cmd.add_argument("--json", action="store_true")

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

    observe_cmd = chain.add_parser("observe", help="layer 1: does the phenomenon recur?")
    observe_cmd.add_argument(
        "--observation", type=Path, default=Path("observation.toml"), help="the declaration"
    )
    observe_cmd.add_argument(
        "--dataset", type=Path, default=None, help="default: the newest dataset in data/"
    )
    observe_cmd.add_argument("--out", type=Path, help="write observation.json here")
    observe_cmd.add_argument("--ledger", type=Path, help="record a death here")
    observe_cmd.add_argument("--id", dest="entry_id", help="ledger entry id for a death")
    observe_cmd.add_argument("--json", action="store_true")

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
    session_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    session_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    session_cmd.add_argument(
        "--dataset",
        action="append",
        default=None,
        metavar="INSTRUMENT=PATH",
        help="default: every dataset in data/, newest per instrument",
    )
    session_cmd.add_argument(
        "--magnitude",
        type=parse_decimal,
        default=None,
        help="the claimed edge, bps; default [agents].magnitude_bps",
    )
    session_cmd.add_argument("--out", type=Path, default=None, help="default: runs/session-<time>")
    session_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    session_cmd.add_argument(
        "--macro", type=Path, help="a series.toml of macro series for the macro analyst"
    )
    session_cmd.add_argument("--onchain", type=Path, help="a series.toml of on-chain series")
    session_cmd.add_argument("--events", type=Path, help="a calendar (JSON or CSV: when, what)")
    session_cmd.add_argument(
        "--observation",
        type=Path,
        default=None,
        help="layer 1 per candidate; default: observation.toml beside the config, if present",
    )
    session_cmd.add_argument("--env-file", type=Path)
    session_cmd.add_argument(
        "--resume", action="store_true", help="reuse the answers and runs already in --out"
    )
    session_cmd.add_argument("--json", action="store_true")

    eval_cmd = agent.add_parser("eval", help="the analyst over one dataset, once per model")
    eval_cmd.add_argument(
        "--model",
        action="append",
        required=True,
        metavar="PROVIDER:MODEL",
        help="a model to compare; repeat for several",
    )
    eval_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    eval_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    eval_cmd.add_argument(
        "--dataset",
        metavar="INSTRUMENT=PATH",
        help="default: the newest dataset in data/ for [run].instrument, or the only one",
    )
    eval_cmd.add_argument(
        "--base-url", help="an OpenAI-compatible endpoint for the compatible providers"
    )
    eval_cmd.add_argument("--out", type=Path, default=None, help="default: runs/eval-<time>")
    eval_cmd.add_argument("--env-file", type=Path)
    eval_cmd.add_argument("--json", action="store_true")

    trace_cmd = agent.add_parser("trace", help="the timeline of a run or a session")
    trace_cmd.add_argument("path", type=Path, help="a run directory or a session directory")
    trace_cmd.add_argument("--json", action="store_true")

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
        cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
        cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
        cmd.add_argument("--out", type=Path, default=None, help="default: runs/<command>-<time>")
        cmd.add_argument("--json", action="store_true")
    for cmd in (replay_cmd, backtest_cmd):
        cmd.add_argument(
            "--parquet", type=Path, default=None, help="default: the newest dataset in data/"
        )
    for cmd in (shadow_cmd, live_cmd, paper_cmd, live_alias):
        cmd.add_argument("--max-reconnects", type=int, default=None)
        cmd.add_argument(
            "--feed", help="live feed plugin; defaults to the venue named in [run].venue_key"
        )
    for cmd in (shadow_cmd, paper_cmd):
        cmd.add_argument(
            "--minutes",
            type=float,
            default=None,
            help="end the session after this long; without it, it runs until you stop it",
        )
    for cmd in (live_cmd, live_alias):
        cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
        cmd.add_argument(
            "--env-file", type=Path, help="KEY=VALUE lines read explicitly; the environment wins"
        )

    mcp_p = top.add_parser("mcp", help="the gates as MCP tools").add_subparsers(
        dest="command", required=True
    )
    mcp_serve_cmd = mcp_p.add_parser(
        "serve", help="serve the read tools over stdio (needs the [mcp] extra)"
    )
    mcp_serve_cmd.add_argument(
        "--root",
        type=Path,
        action="append",
        default=None,
        help="a directory the tools may read under; repeatable; default: the working directory",
    )

    init_cmd = top.add_parser("init", help="a new project directory from the templates")
    init_cmd.set_defaults(command="init")
    init_cmd.add_argument("directory", type=Path)
    init_cmd.add_argument(
        "--agent", metavar="PROVIDER", help="make the technical analyst the core, on this provider"
    )
    init_cmd.add_argument("--model", help="the model for --agent")
    init_cmd.add_argument(
        "--profile",
        choices=("default", "autonomous"),
        default="default",
        help="autonomous lays the cycle down wired: the shelf, the gates and the ladder, live",
    )
    init_cmd.add_argument("--json", action="store_true")

    signals = top.add_parser(
        "signals", help="a run's admitted intents, for execution elsewhere"
    ).add_subparsers(dest="command", required=True)
    export_cmd = signals.add_parser("export", help="signals.jsonl or .csv from a run directory")
    export_cmd.add_argument("run", type=Path, help="a run directory with intents.jsonl")
    export_cmd.add_argument("--out", type=Path, default=None, help="default: <run>/signals.jsonl")
    export_cmd.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    export_cmd.add_argument("--json", action="store_true")

    run_cmd = top.add_parser(
        "run", help="one autonomous cycle: snapshot, agents, cost gate, sandbox intent"
    )
    run_cmd.set_defaults(command="run")
    run_cmd.add_argument("--instrument", default=None, help="default: [run].instrument")
    run_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    run_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    run_cmd.add_argument("--data-config", type=Path, default=Path("data.toml"))
    run_cmd.add_argument(
        "--mode",
        choices=("sandbox", "signals", "venue"),
        default=None,
        help=(
            "sandbox keeps the intent in the cycle, signals writes it for your own executor, "
            "venue sends it; default [execution].mode"
        ),
    )
    run_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    run_cmd.add_argument(
        "--every",
        type=parse_duration_s,
        default=None,
        help="run a cycle every interval, on its boundary (30s, 5m, 1h); one cycle without it",
    )
    run_cmd.add_argument(
        "--cycles",
        type=int,
        default=None,
        help="stop after this many scheduled passes, with --every, cycles or not",
    )
    watch_cmd = top.add_parser(
        "watch", help="a read-only view of a desk from the records it writes; it changes nothing"
    )
    watch_cmd.set_defaults(command="watch")
    watch_cmd.add_argument("--out", type=Path, default=None, help="default: runs/cycles")
    watch_cmd.add_argument(
        "--every", type=parse_duration_s, default=None, help="redraw on an interval (5s, 1m)"
    )
    watch_cmd.add_argument("--limit", type=int, default=12, help="cycles in view; default 12")
    watch_cmd.add_argument("--json", action="store_true")

    keys_cmd = top.add_parser(
        "keys", help="whether every key this project needs is there and works; none is shown"
    )
    keys_cmd.set_defaults(command="keys")
    keys_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    keys_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    keys_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    keys_cmd.add_argument("--env-file", type=Path, help="a .env to read; the shell still wins")
    keys_cmd.add_argument(
        "--all",
        dest="every",
        action="store_true",
        help="every provider this machine holds a key for, not only the ones this project reaches",
    )
    keys_cmd.add_argument(
        "--probe",
        action="store_true",
        help="prove each one works: one free authenticated call per provider, the venue's own "
        "preflight. No prompt is sent and no order is placed",
    )
    keys_cmd.add_argument("--json", action="store_true")

    run_cmd.add_argument(
        "--now",
        action="store_true",
        help="ask the roster whatever [trigger] would have said; the cycle records the override",
    )
    run_cmd.add_argument(
        "--margin", type=parse_decimal, default=None, help="bps the edge must clear beyond cost"
    )
    # One flag per class of data, each pointing at one declaration file. Left alone, a cycle reads
    # the shelf in sources/ beside the config.
    for flag, what in (
        ("macro", "dated levels and rates"),
        ("onchain", "dated chain figures"),
        ("derivatives", "dated funding, open interest and basis"),
        ("sentiment", "dated sentiment scores"),
        ("news", "dated text: releases, statements, your own notes"),
    ):
        run_cmd.add_argument(
            f"--{flag}",
            type=Path,
            default=None,
            help=f"a declaration for {what}; default sources/{flag}.toml",
        )
    run_cmd.add_argument("--out", type=Path, default=None, help="default: runs/cycles")
    run_cmd.add_argument("--env-file", type=Path)
    run_cmd.add_argument("--json", action="store_true")

    check_cmd = top.add_parser("check", help="what is ready and what is missing, in plain words")
    audit_cmd = top.add_parser(
        "audit", help="two auditors over what this project recorded; they propose, you apply"
    )
    audit_cmd.set_defaults(command="audit")
    audit_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    audit_cmd.add_argument("--runs", type=Path, default=Path("runs") / "cycles")
    audit_cmd.add_argument(
        "--sessions",
        type=Path,
        default=Path("runs"),
        help="where closed round trips live, for the quant auditor",
    )
    audit_cmd.add_argument("--out", type=Path, default=None, help="default: audits/")
    audit_cmd.add_argument(
        "--who", choices=("both", "tech_auditor", "quant_auditor"), default="both"
    )
    audit_cmd.add_argument("--env-file", type=Path, default=None)
    audit_cmd.add_argument("--json", action="store_true")

    runs = top.add_parser(
        "runs", help="check a desk's records against their hashes and inputs, or prune old ones"
    ).add_subparsers(dest="command", required=True)
    verify_cmd = runs.add_parser(
        "verify", help="every cycle: files against their hashes, briefs against their inputs"
    )
    verify_cmd.add_argument("--days", type=int, default=None, help="only the last N days")
    prune_cmd = runs.add_parser(
        "prune", help="remove cycle directories older than --keep; the index and logs stay"
    )
    prune_cmd.add_argument("--keep", type=parse_duration, required=True, help="such as 90d")
    prune_cmd.add_argument(
        "--archive", action="store_true", help="zip each month under runs/archive first"
    )
    prune_cmd.add_argument(
        "--yes", action="store_true", help="do it; without this, only say what would go"
    )
    for sub in (verify_cmd, prune_cmd):
        sub.add_argument("--out", type=Path, default=None, help="default: runs/cycles")
        sub.add_argument("--json", action="store_true")

    sources = top.add_parser(
        "sources", help="where the macro, on-chain and sentiment figures come from, and how fresh"
    ).add_subparsers(dest="command", required=True)
    for name, said in (
        ("show", "every declared series: where it comes from, its last value and its age"),
        ("pull", "refresh every series that declares a fetch, now"),
    ):
        sub = sources.add_parser(name, help=said)
        sub.add_argument("--config", type=Path, default=Path("strategy.toml"))
        sub.add_argument("--env-file", type=Path, default=None)
        sub.add_argument("--json", action="store_true")

    venues_cmd = top.add_parser(
        "venues", help="what a venue can do through ccxt: data, orders, exits, account reads"
    )
    venues_cmd.set_defaults(command="venues")
    venues_cmd.add_argument("venue", nargs="*", help="default: the venue in [run].venue_key")
    venues_cmd.add_argument(
        "--setup",
        action="store_true",
        help="print what to paste into envelope.toml, data.toml, strategy.toml and .env",
    )
    venues_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    venues_cmd.add_argument("--json", action="store_true")

    mandate_cmd = top.add_parser(
        "mandate",
        help="print the execution mandate; venue mode needs it in the project, word for word",
    )
    mandate_cmd.set_defaults(command="mandate")
    mandate_cmd.add_argument("--json", action="store_true")

    check_cmd.set_defaults(command="check")
    check_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    check_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    check_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    check_cmd.add_argument("--env-file", type=Path, default=None)
    check_cmd.add_argument("--json", action="store_true")

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
    # Model answers carry any character; a console that cannot show one must not end the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    global _PLAIN
    _PLAIN = bool(args.plain)
    if args.once is not None:
        return _console_for(parser, args, interactive=False).one(args.once, say=False) or CLEARED
    if args.group is None and console.interactive() and not _PLAIN:
        # A person at a terminal gets somewhere to stand, not one screen and their shell back.
        return _console_for(parser, args).loop()
    handler = cockpit if args.group is None else HANDLERS[(args.group, args.command)]
    return _dispatch(handler, args)


def _set_plain(plain: bool) -> None:
    global _PLAIN
    _PLAIN = plain


def _console_for(
    parser: argparse.ArgumentParser, args: argparse.Namespace, *, interactive: bool = True
) -> console.Console:
    """The console, wired to the same parser and the same handlers a shell would reach.

    `tra -c` runs one command and leaves, so it is given no reader and no history: a one-shot in a
    script has nothing to arrow back through, and a history file it never types into is a file it
    has no business opening.
    """
    return console.Console(
        parser=parser,
        run=lambda parsed: _dispatch(HANDLERS[(parsed.group, parsed.command)], parsed),
        cockpit=lambda: cockpit(args),
        commands=[f"{group} {command}" for group, command in sorted(HANDLERS)],
        set_plain=_set_plain,
        history=console.history_path(bool(args.history)) if interactive else None,
        read=None if interactive else (lambda: ""),
    )


def _dispatch(handler: Callable[[argparse.Namespace], int], args: argparse.Namespace) -> int:
    """Run one command and turn the failures this project knows about into one line.

    The console and the shell share this, so a command behaves the same in both: a refusal is a
    refusal, not a traceback, and nothing here decides what a command does.
    """
    try:
        return handler(args)
    except BrokenPipeError:
        # `tra ... | head`: the reader closed; nothing was refused.
        return CLEARED
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
        ChainExhaustedError,
        DataConfigError,
        SourceError,
        BriefError,
        ContextError,
        CycleError,
        PreTradeError,
        UnknownSourceError,
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
        if "no funding rate" in str(exc):
            print(
                "tra: the run spans a settlement; `tra data get` fetches funding with the day, "
                "or run `tra data funding` and set [run].funding",
                file=sys.stderr,
            )
        return REFUSED


_PLAIN = False


def _watching(as_json: bool = False) -> bool:
    """A person at a terminal, who gets the console view instead of the lines."""
    from trading_research_agents.cli import tui

    return tui.enabled(_PLAIN, as_json)


def emit(
    payload: dict[str, Any],
    as_json: bool,
    lines: list[str],
    render: Callable[[], Any] | None = None,
) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str), flush=True)
    elif render is not None and _watching():
        from trading_research_agents.cli import tui

        tui.show(render())
    else:
        # Flushed per report: a desk logging to a file is read while it runs, not after it stops.
        print(plain_text("\n".join(lines)), flush=True)


# Typographic characters models like to write. A log or an old console shows them as escapes or
# replacement marks, so plain output carries their ASCII form; the records keep the original.
TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u202f": " ",
    }
)


def plain_text(text: str) -> str:
    return text.translate(TYPOGRAPHY)


def cockpit(args: argparse.Namespace) -> int:
    """`tra` alone: the banner, what is ready, and the one command to type next."""
    from trading_research_agents.cli import tui

    defaults = argparse.Namespace(
        config=Path("strategy.toml"),
        envelope=Path("envelope.toml"),
        ladder=Path("ladder.json"),
        env_file=None,
        json=False,
    )
    good, bad, next_command = _check(defaults)
    if not _watching():
        print(f"tra {__version__}")
        print("\n".join([*(f"ok    {g}" for g in good), *(f"todo  {b}" for b in bad)]))
        print(f"next: {next_command}")
        return CLEARED
    tui.cockpit(__version__, good, bad, next_command)
    return CLEARED


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


def chain_observe(args: argparse.Namespace) -> int:
    from trading_research_agents.chain.observation import (
        load_observation,
        observe,
        write_report,
    )
    from trading_research_agents.data.parquet import read_trades as read_prints

    observation = load_observation(args.observation)
    dataset = args.dataset
    if dataset is None:
        found = _datasets(args.observation)
        if len(found) != 1:
            where = _data_dir(args.observation)
            raise ValueError(f"{len(found)} instruments in {where}; say which: --dataset PATH")
        (dataset,) = found.values()
    report = observe(observation, read_prints(dataset), dataset=str(dataset))
    r = report.response_bps
    lines = [
        f"{report.recurrence.value.upper()}: {report.events} events over {report.bars} bars "
        f"({report.measured} measured, {report.skipped} skipped), per subperiod "
        f"{list(report.events_per_subperiod)}",
    ]
    if r["n"]:
        lines.append(
            f"response at {observation.horizon_bars} bars: median {r['median']} bps, mean "
            f"{r['mean']} bps, q25 {r['q25']}, q75 {r['q75']}, positive {r['fraction_positive']}, "
            f"median |response| {r['median_abs']} bps"
        )
    for fired in report.fired:
        lines.append(f"  fired {fired}")
    note = report.record()["magnitude"]
    if note:
        lines.append(note)
    recorded = None
    if not report.recurs and args.ledger is not None:
        if not args.entry_id:
            raise ValueError("--ledger needs --id for the new row")
        entry = report.to_entry(
            entry_id=args.entry_id, recorded_at=datetime.now(UTC), evidence=(str(dataset),)
        )
        Ledger(args.ledger).append(entry)
        recorded = entry.entry_id
        lines.append(f"recorded {entry.entry_id} as {entry.verdict.value} in {args.ledger}")
    if args.out is not None:
        write_report(report, args.out)
        lines.append(f"observation.json in {args.out.parent}")
    payload = {**report.record(), "recorded": recorded}
    emit(payload, args.json, lines, lambda: _ui().observe_view(payload))
    return CLEARED if report.recurs else REFUSED


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
    from trading_research_agents.agents.strategy import AgentStrategy
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    a = cfg.agents
    completer = _build_completer(a)
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
        decide_every_bars=a.decide_every_bars,
    )


def _build_completer(a: Any) -> Completer:
    p = provider(a.provider)
    if p.build_with is not None:
        return p.build_with(a.model, a.base_url, _call_options(a))
    return p.build(a.model, a.base_url)


# Measured twice, on two different models: a schema with several cited claims in it needs about
# twice the output cap a one-line answer does, and below this a model returns an invalid object
# rather than a short one. The floor is announced when it is applied, never applied silently.
REPORT_HEADROOM = 2048


def _completer_for_role(
    a: Any,
    role: str,
    *,
    floor: int = 0,
    on_attempt: Callable[[str, Any], None] | None = None,
) -> Completer:
    """The models this role is asked with, in the order the operator declared them.

    A screener that labels a regime and an analyst that reasons over sixty bars do not need the
    same model, and the difference is most of a token bill. `[agents.roles.<role>]` is where an
    operator says so, and `fallback` is where they say what to do when that provider is rate
    limited or down: the next one is tried with the same brief, because the brief is built by code
    and there is no conversation to carry over.
    """
    settings = a.for_role(role)
    links = [Link(f"{settings.provider}:{settings.model or 'default'}", _link(a, settings, floor))]
    for name in settings.fallback:
        named, model = parse_link(name)
        links.append(Link(name, _link(a, settings, floor, provider_name=named, model=model)))
    wait = float(getattr(a, "max_retry_wait_s", 0.0) or 0.0)
    if len(links) == 1 and wait <= 0:
        return links[0].build()
    return CompleterChain(role=role, links=links, on_attempt=on_attempt, max_wait_s=wait)


def _link(
    a: Any,
    settings: Any,
    floor: int,
    *,
    provider_name: str | None = None,
    model: str | None = None,
) -> Callable[[], Completer]:
    """How one link is built, when it is first needed rather than when it is declared."""

    def build() -> Completer:
        p = provider(provider_name or settings.provider)
        declared = settings.max_output_tokens
        options = CallOptions(
            # No cap stays no cap: the floor exists to raise one an operator set too low, and a
            # report needs more room than a one-line answer does.
            max_output_tokens=None if declared is None else max(declared, floor),
            timeout_s=float(settings.call_timeout_s),
            max_retries=settings.max_retries,
            effort=settings.effort,
        )
        wanted = model if provider_name else settings.model
        base = None if provider_name else settings.base_url
        if p.build_with is not None:
            return p.build_with(wanted, base, options)
        return p.build(wanted, base)

    return build


def _call_options(a: Any) -> CallOptions:
    return CallOptions(
        max_output_tokens=a.max_output_tokens,
        timeout_s=float(a.call_timeout_s),
        max_retries=a.max_retries,
        effort=a.effort,
    )


def _trading_config(
    args: argparse.Namespace,
    feed: Any,
    label: str,
    *,
    venue: Venue | None = None,
    canary: CanaryGuard | None = None,
    observer: Any = None,
) -> TradingConfig:
    cfg = load_strategy_config(args.config)
    strategy = cfg.strategy if cfg.agents is None else _agent_core(cfg, args.out)
    funding = cfg.funding
    if funding is None:
        beside = _data_dir(args.config) / f"{cfg.run.instrument}-funding.json"
        if beside.is_file():
            funding = load_funding(beside, cfg.run.settlement_interval_s)
    if venue is None:
        venue = SimulatedVenue(
            channel=_channel(args, cfg),
            funding=funding or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
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
        size=cfg.run.fixed_size(),
        spread_cost_bps=cfg.run.spread_cost_bps,
        equity=cfg.run.equity,
        label=label,
        canary=canary,
        observer=observer,
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
    emit(payload, args.json, lines, lambda: _ui().run_panel(report, str(args.out)))
    return CLEARED if report.stopped_by is None else REFUSED


def _ui() -> Any:
    from trading_research_agents.cli import tui

    return tui


def _run_watched(args: argparse.Namespace, build: Callable[[Any], TradingConfig]) -> Any:
    """Run a trading config; when a person is watching, a live panel follows every bar."""
    if not _watching(args.json):
        return run_trading(build(None))
    with _ui().RunView(str(args.out)) as view:
        return run_trading(build(view.observe))


def _observation_beside(config: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    beside = config.resolve().parent / "observation.toml"
    return beside if beside.is_file() else None


def _data_dir(config: Path) -> Path:
    return config.resolve().parent / "data"


def _datasets(config: Path) -> dict[str, Path]:
    """`data/<INSTRUMENT>-<when>.parquet` as `tra data get` writes them: newest per instrument."""
    found: dict[str, Path] = {}
    for path in sorted(_data_dir(config).glob("*.parquet")):
        instrument = path.stem.split("-", 1)[0]
        found[instrument] = path
    return found


def _run_dir(args: argparse.Namespace, label: str) -> Path:
    if args.out is None:
        args.out = Path("runs") / f"{label}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    out: Path = args.out
    return out


def trade_replay(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "backtest")
    parquet = args.parquet
    if parquet is None:
        parquet = _datasets(args.config).get(cfg.run.instrument)
        if parquet is None:
            raise ValueError(
                f"no dataset for {cfg.run.instrument} in {_data_dir(args.config)}; "
                f"run `tra data get {cfg.run.instrument} YYYY-MM-DD` or pass --parquet"
            )
    feed = RecordedFeed.from_parquet(parquet, instrument=cfg.run.instrument)
    return _report(
        args, _run_watched(args, lambda o: _trading_config(args, feed, "replay", observer=o))
    )


def trade_shadow(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "paper")
    # The channel key is venue/channel; the venue half names the feed unless told otherwise.
    feed = _live_feed(args, cfg, args.feed or cfg.run.venue_key.partition("/")[0])
    return _report(
        args, _run_watched(args, lambda o: _trading_config(args, feed, "shadow", observer=o))
    )


def _live_feed(args: argparse.Namespace, cfg: Any, name: str) -> Any:
    """The public feed, and the clock that ends the session when the operator named one.

    A shadow session runs until it is stopped, which is right for a desk and wrong for the first
    one anybody runs: `--minutes` gives it an end without anybody having to reach for Ctrl-C.
    """
    stop = threading.Event()
    minutes = getattr(args, "minutes", None)
    if minutes:
        ending = threading.Timer(float(minutes) * 60.0, stop.set)
        ending.daemon = True
        ending.start()
    return live_feed(name)(cfg.run.instrument, stop=stop, max_reconnects=args.max_reconnects)


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


def parse_duration_s(text: str) -> float:
    """`30s`, `5m`, `2h` to seconds, for a flag. The same spellings data.toml already takes."""
    from trading_research_agents.data.sources import parse_duration

    seconds = parse_duration(text)
    if seconds < 5:
        raise argparse.ArgumentTypeError("an interval below five seconds is not a trading cadence")
    return seconds


def _load_env(args: argparse.Namespace) -> None:
    """Keys into this process: the file the operator named, then the project's own `.env`.

    The shell always wins. Nothing outside those two files is read, and a command that finds no key
    still says which name it was looking for rather than failing at the vendor.
    """
    beside = getattr(args, "config", None)
    here = beside.resolve().parent / ".env" if beside is not None else None
    for path in (getattr(args, "env_file", None), here):
        if path is None or not path.is_file():
            continue
        for key, value in read_env_file(path).items():
            os.environ.setdefault(key, value)


def _environment(env_file: Path | None, *, beside: Path | None = None) -> dict[str, str]:
    """The environment, plus the file the operator named, plus the project's own `.env`.

    A key in the shell wins over a file, and a file the operator named wins over the project's own.
    `.env` beside the config is read because that is where `tra init` tells them to put it, and
    because a check that says "no key" while the key sits in the next file is a check nobody trusts.
    Which file was read is reported, so nothing is picked up silently.
    """
    env = dict(os.environ)
    for path in (env_file, (beside / ".env") if beside is not None else None):
        if path is None or not path.is_file():
            continue
        for key, value in read_env_file(path).items():
            env.setdefault(key, value)
    return env


def _env_file_used(env_file: Path | None, beside: Path | None) -> Path | None:
    """The file a check actually read a key from, for the line that says so."""
    for path in (env_file, (beside / ".env") if beside is not None else None):
        if path is not None and path.is_file():
            return path
    return None


def trade_live(args: argparse.Namespace) -> int:
    cfg = load_strategy_config(args.config)
    _run_dir(args, "live")
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
    guard = CanaryGuard(cfg.canary)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "preflight.json").write_text(
        json.dumps({"run_id": run_id, "venue": venue.label, **checks}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    report = _run_watched(
        args,
        lambda o: _trading_config(args, feed, "live-canary", venue=venue, canary=guard, observer=o),
    )
    events = getattr(venue, "events", [])
    if events:
        (args.out / "venue_events.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    return _report(args, report)


TEMPLATE_FILES = {
    "strategy.toml": "strategy.toml",
    "envelope.toml": "envelope.toml",
    "mapping.toml": "mapping.toml",
    "data.toml": "data.toml",
    "observation.toml": "observation.toml",
    # One declaration per class of data, so a vendor is a line in a file rather than a file of
    # its own. The analysts that read them are asked only when their class is declared.
    "sources/macro.toml": "sources/macro.toml",
    "sources/onchain.toml": "sources/onchain.toml",
    "sources/derivatives.toml": "sources/derivatives.toml",
    "sources/sentiment.toml": "sources/sentiment.toml",
    "sources/news.toml": "sources/news.toml",
    "strategies/trailing_zscore.py": "core.py.tmpl",
    ".env.example": "env.example",
    ".gitignore": "gitignore",
    "README.md": "README.md",
}


def _metered_roles(a: Any, usage: Any, *, floor: int = 0) -> Callable[[str], Any]:
    """One metered completer per role, built when the role is first asked.

    A role may run on its own model without the others paying for it, and every call goes through
    the same ledger, so what a desk spent is one file however many roles were involved.
    """
    from trading_research_agents.llm.usage import BudgetLimits, MeteredCompleter, Pricing

    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {model: Pricing(i, o) for model, (i, o) in a.pricing.items()}
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        if role not in metered:
            settings = a.for_role(role)
            metered[role] = MeteredCompleter(
                _completer_for_role(a, role, floor=floor),
                role=role,
                provider=settings.provider,
                limits=limits,
                ledger=usage,
                pricing=pricing,
            )
        return metered[role]

    return completer_for


def _audit_lines(result: Any, raised: list[str]) -> list[str]:
    """One audit as the lines a person reads, used by the command and by a scheduled run."""
    lines = [f"audit {result.at}: {result.cycles} cycle(s) read, {result.findings} finding(s)"]
    if raised:
        lines.append(
            f"output cap raised to {REPORT_HEADROOM} for {', '.join(raised)}: a report with "
            "findings in it does not fit in less"
        )
    for role, report in sorted(result.reports.items()):
        lines.append(f"{role.replace('_', ' ')}: {report.summary}")
        for finding in report.findings:
            lines.append(f"  - {finding.what} ({finding.confidence})")
            lines.append(f"    proposal: {finding.proposal}")
            if finding.change is not None:
                lines.append(
                    f"    change: {finding.change.key} "
                    f"{finding.change.from_value} -> {finding.change.to_value}"
                )
        for reason in report.discarded:
            lines.append(f"  discarded: {reason}")
    for role, error in sorted(result.failed.items()):
        lines.append(f"{role}: {error}")
    lines.append(f"proposals.md and audit.json in {result.path}; nothing has been applied")
    return lines


def run_audit_command(args: argparse.Namespace) -> int:
    """Two auditors over the records this project already wrote. Nothing is applied."""
    from trading_research_agents.agents.audit import AuditConfig, run_audit
    from trading_research_agents.llm.usage import UsageLedger

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("an audit needs [agents] in strategy.toml; it asks a model")
    a = cfg.agents
    out = args.out or Path("audits")
    usage = UsageLedger(out / "usage.jsonl")
    completer_for = _metered_roles(a, usage, floor=REPORT_HEADROOM)

    who = ("tech_auditor", "quant_auditor") if args.who == "both" else (args.who,)
    # Only a declared cap can be too low; an undeclared one is the vendor's own maximum.
    raised = [
        r for r in who if (a.for_role(r).max_output_tokens or REPORT_HEADROOM) < REPORT_HEADROOM
    ]
    result = run_audit(
        AuditConfig(
            runs=args.runs,
            completer_for=completer_for,
            usage=usage,
            root=out,
            config=args.config,
            sessions=args.sessions,
            auditors=who,
        )
    )
    payload = result.record()
    payload["out"] = str(result.path)
    emit(payload, args.json, _audit_lines(result, raised), lambda: _ui().audit_view(payload))
    return CLEARED


def watch_desk(args: argparse.Namespace) -> int:
    """What a desk is doing, read from its own records. Nothing here can reach it.

    This is the answer to "can I watch a running desk without disturbing it": the command opens no
    socket, builds no client, holds no key and takes no lock. Closing it is closing a reader.
    """
    from trading_research_agents.cli.watch import look, short_time

    out = args.out or Path("runs") / "cycles"
    if not out.is_dir():
        raise ConfigError(f"{out}: no cycles have been written here yet; run `tra run` first")

    def desk() -> dict[str, Any]:
        return look(out, limit=max(1, args.limit)).record()

    if args.json or args.every is None or not _watching(args.json):
        payload = desk()
        lines = [
            f"{short_time(row['at'])}  {row['instrument']:<9} {row['outcome']:<11} {row['detail']}"[
                :120
            ]
            for row in payload["decisions"]
        ]
        lines.append(f"reading {out}; this view only reads, and changes nothing")
        emit(payload, args.json, lines, lambda: _ui().watch_view(payload))
        return CLEARED

    with _ui().WatchView(desk, every_s=args.every) as view:
        try:
            while True:
                sleep(args.every)
                view.draw()
        except KeyboardInterrupt:
            pass
    print(f"stopped watching {out}; nothing about the desk changed")
    return CLEARED


def show_keys(args: argparse.Namespace) -> int:
    """Every credential this configuration would reach, and whether it is in a position to work."""
    from trading_research_agents.cli.credentials import Readiness, in_play, provider_row, venue_row

    _load_env(args)
    beside = args.config.resolve().parent
    env = _environment(args.env_file, beside=beside)
    out = Readiness()
    cfg = None
    if args.config.is_file():
        try:
            cfg = load_strategy_config(args.config)
        except (ConfigError, OSError, ValueError) as exc:
            print(f"tra: {args.config}: {exc}", file=sys.stderr)
    wanted: list[tuple[str, str]] = []
    if cfg is not None and cfg.agents is not None and not args.every:
        wanted = in_play(cfg.agents)
    else:
        # No project, no agents in it, or --all: say what this machine is holding keys for.
        reached = dict(in_play(cfg.agents)) if cfg is not None and cfg.agents is not None else {}
        wanted = [
            (name, reached.get(name, "a key is named for it"))
            for name in providers()
            if _named(name, env)
        ]
    for name, why in wanted:
        try:
            p = provider(name)
        except UnknownProviderError as exc:
            out.rows.append(_unknown_provider(name, why, str(exc)))
            continue
        out.rows.append(
            provider_row(p, env, label=f"provider {name} ({why})", verify=bool(args.probe))
        )
    if cfg is not None and cfg.live is not None:
        venue = live_venue(cfg.live.venue)
        out.rows.append(
            venue_row(
                venue,
                env,
                label=f"venue {cfg.live.venue} ({cfg.live.environment}), for `tra live`",
                preflight=_venue_preflight(cfg, env) if args.probe else None,
            )
        )
    if cfg is not None and cfg.execution.places_orders:
        # The cycle sends through a different client than a live session does, so a probe that only
        # checked the session's client would be checking a path no order takes.
        out.rows.append(
            venue_row(
                _DispatchKeys(cfg.execution.venue),
                env,
                label=f"venue {cfg.execution.venue} ({_declared_environment(cfg)}), for `tra run`",
                preflight=_dispatch_ready(args, cfg) if args.probe else None,
            )
        )
    lines = []
    for row in out.rows:
        state = "ok" if row.ready else "todo"
        held = ", ".join(
            f"{name} {fp}" for name, fp in zip(row.present, row.fingerprints, strict=False)
        )
        lines.append(f"{state:<5} {row.what}: {row.verdict}" + (f" [{held}]" if held else ""))
    lines.append(
        "every key above is shown as a fingerprint of itself, never as its value"
        if out.rows
        else "no credential is declared or set for this project"
    )
    if not args.probe and out.rows:
        lines.append("`tra keys --probe` proves each one works: no prompt is sent, no order placed")
    emit(out.record(), args.json, lines, lambda: _ui().keys_table(out.record()))
    return CLEARED if out.ready else REFUSED


def _named(name: str, env: Mapping[str, str]) -> bool:
    """Whether this machine has a variable for this provider at all, filled in or not.

    An empty one is worth showing rather than hiding: somebody meant to use that provider and has
    not finished, and a list that quietly leaves it out is how they find out mid-cycle.
    """
    try:
        p = provider(name)
    except UnknownProviderError:
        return False
    return bool(p.key_env and p.key_env in env)


def _unknown_provider(name: str, why: str, said: str) -> Any:
    from trading_research_agents.cli.credentials import Credential

    return Credential(what=f"provider {name} ({why})", env=(), missing=(name,), note=said)


@dataclass(frozen=True)
class _DispatchKeys:
    """The key names the cycle's own hand reads, named the way that hand names them."""

    venue: str

    @property
    def env_keys(self) -> tuple[str, ...]:
        from trading_research_agents.trading.dispatch import credentials_for

        return tuple(credentials_for(self.venue).values())


def _declared_environment(cfg: Any) -> str:
    return cfg.live.environment if cfg.live is not None else "demo"


def _dispatch_ready(args: argparse.Namespace, cfg: Any) -> Callable[[], Any]:
    """Read the account through the very client an order would go through. Nothing is sent."""

    def run() -> Any:
        hand = _venue_hand(args, cfg, args.config.resolve().parent)
        said = hand.ready()
        if said:
            raise RuntimeError(said)
        return said

    return run


def _venue_preflight(cfg: Any, env: Mapping[str, str]) -> Callable[[], Any]:
    """The venue's own preflight, as the readiness check runs it: no order, no session."""

    def run() -> Any:
        settings = LiveVenueSettings(
            instrument=cfg.run.instrument,
            channel=cfg.run.venue_key.partition("/")[2] or cfg.run.venue_key,
            funding=cfg.funding
            or FundingSchedule(interval_s=cfg.run.settlement_interval_s, rates={}),
            environment=cfg.live.environment,
            run_id="readiness",
            key_ip_unbound_ok=cfg.live.key_ip_unbound_ok,
            adopt_open_position=True,
            poll_interval_ms=cfg.live.poll_interval_ms,
        )
        return live_venue(cfg.live.venue)(settings, env).preflight()

    return run


def runs_verify(args: argparse.Namespace) -> int:
    """Whether the desk's records are what it wrote, and its briefs what its inputs give."""
    from trading_research_agents.agents.verify import verify_runs

    found = verify_runs(args.out or Path("runs") / "cycles", days=args.days)
    lines = [
        f"{found['checked']} of {found['cycles']} cycle(s) checked: {found['hashed']} against "
        f"their hashes, {found['recomputed']} recomputed from their inputs; "
        f"{found['pruned']} pruned (their index lines remain)"
    ]
    for failed in found["failed"]:
        lines.extend(f"FAILED {failed['cycle']}: {problem}" for problem in failed["problems"])
    if not found["failed"]:
        lines.append("no record differs from what the desk wrote")
    emit(found, args.json, lines)
    return 1 if found["failed"] else 0


def runs_prune(args: argparse.Namespace) -> int:
    """Old cycle directories out, only when asked twice: once to see, once with --yes."""
    from trading_research_agents.store.prune import apply, plan

    root = args.out or Path("runs") / "cycles"
    chosen = plan(root, args.keep)
    found = chosen.record()
    megabytes = chosen.size / 1_000_000
    said = (
        f"{len(chosen.folders)} cycle director(ies) and {len(chosen.screens)} screen(s) older than "
        f"{chosen.cutoff}, {megabytes:.1f} MB"
    )
    if not args.yes:
        emit({**found, "applied": False}, args.json, [f"would remove {said}; add --yes to do it"])
        return 0
    kept = apply(root, chosen, archive=args.archive)
    where = f"; archived under {kept}" if kept else ""
    emit(
        {**found, "applied": True, "archive": str(kept) if kept else None},
        args.json,
        [f"removed {said}{where}; the index and the logs are kept"],
    )
    return 0


def _shelf_series(args: argparse.Namespace) -> list[Path]:
    shelf = declared_in(args.config.resolve().parent)
    return [path for name, path in sorted(shelf.classes.items()) if name not in TEXT_CLASSES]


def sources_show(args: argparse.Namespace) -> int:
    """Each declared series, so nobody has to take an analyst's word for what it read."""
    from trading_research_agents.data.pull import describe
    from trading_research_agents.data.series import load_series_map

    now = datetime.now(UTC)
    rows = [
        {"file": str(path.name), **describe(spec, path.resolve().parent, now)}
        for path in _shelf_series(args)
        for spec in load_series_map(path)
    ]
    lines = []
    for row in rows:
        if "error" in row:
            state = f"unreadable: {row['error']}"
        else:
            state = f"last {row['last'][:16]} = {row['value']}, {row['age_h']}h old" + (
                "" if row["counted"] else f", past max_age {row['max_age']}: not counted"
            )
        lines.append(f"{row['series']:<20} {row['from']}; {row['source'] or '-'}; {state}")
    if not rows:
        lines.append("no series declared under sources/; the reading analysts are not asked")
    emit({"series": rows}, args.json, lines)
    return 0 if all(r.get("counted") for r in rows) else 1


def sources_pull(args: argparse.Namespace) -> int:
    """Refresh every fetched series now, whatever its refresh interval says."""
    from trading_research_agents.data.pull import PullError, pull
    from trading_research_agents.data.series import load_series_map

    _load_env(args)
    rows: list[dict[str, Any]] = []
    for path in _shelf_series(args):
        for spec in load_series_map(path):
            if spec.fetch is None:
                rows.append({"series": spec.name, "skipped": "kept by hand; no fetch declared"})
                continue
            try:
                got = pull(spec, path.resolve().parent)
            except PullError as exc:
                rows.append({"series": spec.name, "error": str(exc)})
            else:
                rows.append(
                    {"series": got.series, "added": got.added, "rows": got.rows, "last": got.last}
                )
    lines = [
        f"{r['series']:<20} "
        + (
            f"{r['added']} new, {r['rows']} rows, last {r['last']}"
            if "added" in r
            else r.get("skipped") or f"failed: {r['error']}"
        )
        for r in rows
    ]
    emit({"pulled": rows}, args.json, lines or ["no series declared under sources/"])
    return 1 if any("error" in r for r in rows) else 0


def show_venues(args: argparse.Namespace) -> int:
    """What a venue can actually do, asked of ccxt rather than promised in a README."""
    from trading_research_agents.data.sources.capability import CapabilityError, capability_of
    from trading_research_agents.trading.dispatch import credentials_for

    names = list(args.venue)
    if not names and args.config.is_file():
        raw = tomllib.loads(args.config.read_text(encoding="utf-8"))
        declared = str(raw.get("run", {}).get("venue_key", "")).partition("/")[0]
        names = [declared] if declared else []
    if not names:
        raise ConfigError("name a venue, or run this inside a project with [run].venue_key")

    found = []
    lines: list[str] = []
    for name in names:
        try:
            able = capability_of(name)
        except CapabilityError as exc:
            lines.append(f"{name}: {exc}")
            continue
        found.append(able.record())
        serves = ", ".join(able.serves_data) or "nothing this project reads"
        lines.append(f"{able.venue}")
        lines.append(f"  data        {serves}")
        missing = [kind for kind, ok in sorted(able.data.items()) if not ok]
        if missing:
            lines.append(f"  no data for {', '.join(missing)}; do not declare those in data.toml")
        lines.append(
            "  execution   "
            + ", ".join(
                f"{what}: {'yes' if able.execution.get(what) else 'no'}"
                for what in ("create_order", "attached_exits", "position", "open_orders", "cancel")
            )
        )
        lines.append(f"  practice    {able.practice}")
        keys = list(credentials_for(able.venue).values())
        lines.append(f"  keys        {', '.join(keys)}")
        lines.append(f"  verdict     {able.verdict()}")
        if args.setup:
            snippet = venue_setup(able, keys)
            found[-1]["setup"] = snippet
            lines.extend(["", *snippet.splitlines(), ""])
    emit({"venues": found}, args.json, lines)
    return CLEARED


def venue_setup(able: Any, keys: list[str]) -> str:
    """The blocks an operator pastes to trade on a venue, shaped by what the venue can do.

    The fees are left blank on purpose. A fee is the largest figure in the cost gate, it depends on
    the account's own tier, and a placeholder number would price every trade at a rate nobody
    checked; a blank one makes the file refuse to load until somebody writes theirs in.
    """
    name = able.venue
    serves = list(able.serves_data)
    environment = "demo" if able.demo else "testnet" if able.testnet else "mainnet"
    out = [f"# {name}: {able.verdict()}"]
    if able.level == "data":
        out += [
            "# This venue can feed a cycle's figures but not take its orders. In data.toml,",
            f'# put "ccxt:{name}" in the chain of: {", ".join(serves) or "nothing this reads"}.',
        ]
        return "\n".join(out)
    out += [
        "",
        "# 1. envelope.toml: what a round trip costs here. Write your own account's fees, in bps",
        "#    (0.055% is 5.5); the file will not load while they are blank.",
        "[[channel]]",
        f'venue = "{name}"',
        'name = "taker-taker"',
        "entry_fee_bps =                  # your taker fee",
        "exit_fee_bps =                   # your taker fee",
        "",
        "[[channel]]",
        f'venue = "{name}"',
        'name = "maker-taker"',
        "entry_fee_bps =                  # your maker fee",
        "exit_fee_bps =                   # your taker fee",
        "",
        f'# 2. data.toml: put "ccxt:{name}" first in the chain of {", ".join(serves)}.',
        "#    Each instrument may need its ccxt symbol under [data].symbols.",
        "",
        "# 3. strategy.toml",
        "[run]",
        f'venue_key = "{name}/taker-taker"',
        "size_step = { BTCUSDT = 0.001 }   # this venue's increment per instrument; an order off",
        "                                 # it is refused, and the refusal names the right step",
        "",
        "[execution]",
        f'venue = "{name}"',
        "leverage = 3                     # yours to choose; applied at the venue and read back",
        'margin_mode = "isolated"         # isolated keeps one position from drawing on the rest',
    ]
    if able.level == "exits-none":
        out += [
            'exits = "none"                   # this venue cannot attach a stop to the entry;',
            "                                 # you carry the stop and take profit yourself",
        ]
    out += ["", "[live]", f'environment = "{environment}"']
    if environment == "mainnet":
        out.append("# no practice environment through ccxt: the first order here is real money")
    out += ["", "# 4. .env beside the config, never in a file you commit", *(f"{k}=" for k in keys)]
    out += ["", f"# Then: tra check, and tra keys --probe to prove the keys reach {name}."]
    return "\n".join(out)


def show_mandate(args: argparse.Namespace) -> int:
    """The text venue mode needs, printed so an operator can read it and keep it."""
    from trading_research_agents.execution.mandate import DISCLAIMER, MANDATE_FILE

    if args.json:
        emit({"mandate": DISCLAIMER, "file": MANDATE_FILE}, True, [])
        return CLEARED
    sys.stdout.write(DISCLAIMER)
    return CLEARED


def init_project(args: argparse.Namespace) -> int:
    target: Path = args.directory
    templates = files("trading_research_agents").joinpath("templates")
    existing = [name for name in TEMPLATE_FILES if (target / name).exists()]
    if existing:
        raise ValueError(f"{target}: already has {existing}; init never overwrites")
    autonomous = args.profile == "autonomous"
    if args.agent and not args.model:
        raise ValueError("--agent needs --model")
    if args.agent:
        provider(args.agent)
    if autonomous and not args.agent:
        # The profile exists to save an operator the assembly, not to guess which model they pay
        # for. It lays the gates down live and leaves the provider for them to name.
        args.agent, args.model = "", ""
    for name, source in TEMPLATE_FILES.items():
        out = target / name
        out.parent.mkdir(parents=True, exist_ok=True)
        text = templates.joinpath(*source.split("/")).read_text(encoding="utf-8")
        if name == "strategy.toml" and (args.agent or autonomous):
            text = _agent_config(text, args.agent or "your-provider", args.model or "your-model")
        if name == "strategy.toml" and autonomous:
            text = text.replace("\n[data]\n", AUTONOMOUS_GATES + "[data]\n", 1)
        if name == "README.md" and autonomous:
            text = text.rstrip() + FIRST_HOUR
        out.write_text(text, encoding="utf-8")
    lines = [f"{target}: {len(TEMPLATE_FILES)} files", *(f"  {n}" for n in TEMPLATE_FILES)]
    if args.agent:
        key = provider(args.agent).key_env
        if key:
            lines.append(f"put {key} in .env (copy .env.example)")
    if autonomous:
        lines.append(
            "the cycle is wired: the shelf, the matrix, the conviction ladder, the targets"
        )
        lines.append("next: put your key in .env, name the provider in [agents], then `tra check`")
    else:
        lines.append("next: cd there, `tra data get BTCUSDT 2025-01-01`, then `tra backtest`")
    emit({"directory": str(target), "files": list(TEMPLATE_FILES)}, args.json, lines)
    return CLEARED


# What `tra init --profile autonomous` writes where the operator's own provider goes. `tra check`
# names it rather than passing the vendor's "no such provider" error through.
PLACEHOLDER_PROVIDER = "your-provider"

AGENT_HEAD = (
    "[strategy]\n"
    'agent = "technical_analyst"          # the model decides on each closed bar\n'
    "# file = ... / class = ...             # or a code core instead (docs/trading.md)\n\n"
)


def _without_commented(text: str, marker: str) -> str:
    """Drop one commented-out block, from its marker to the blank line that ends it.

    The template is a page an operator reads, so a block that has been replaced by a live table
    should go, and every block after it should stay. Cutting to the end of the file would take the
    gates and the data section with it.
    """
    if marker not in text:
        return text
    start = text.index(marker)
    lines = text[start:].splitlines(keepends=True)
    taken = 0
    for line in lines:
        if line.strip() == "":
            break
        taken += 1
    return text[:start] + "".join(lines[taken:]).lstrip("\n")


def _agent_config(text: str, provider_name: str, model: str) -> str:
    """The template with the technical analyst as the core and the [agents] table live."""
    text = text[: text.index("[strategy]")] + AGENT_HEAD + text[text.index("[exit]") :]
    text = _without_commented(text, "# [agents]")
    agents = (
        "[agents]\n"
        f'provider = "{provider_name}"\n'
        f'model = "{model}"\n'
        'mechanism = "state the mechanism the analyst reasons from"\n'
        "lookback_bars = 60\n"
        "min_confidence = 0.6\n"
        "magnitude_bps = 20                 # the claimed edge, for the cost floor\n"
        '# effort = "medium"                # minimal .. max, where your vendor takes one\n'
        "\n"
        "# Nothing caps what a role may spend: every call is counted in usage.jsonl\n"
        "# and each cycle reports what it cost. Declare token_budget or\n"
        "# max_calls_per_hour only if you want a hard stop, and know that it stops\n"
        "# the desk rather than slowing it down.\n"
        "\n"
        f"# [agents.pricing.{model}]\n"
        "# input_per_million = 0.0          # what you were quoted, per million tokens\n"
        "# output_per_million = 0.0\n\n"
    )
    return text.replace("[data]\n", agents + "[data]\n", 1)


AUTONOMOUS_GATES = """
[confluence]
# Which reading analysts must agree with the direction. Empty until you declare their sources in
# sources/, because a role that was never asked has agreed to nothing and this gate fails closed.
required = []
long_states = ["bullish"]
short_states = ["bearish"]
on_missing = "refuse"
min_support = 0                    # validators that must support it; needs [agents].validators

[conviction]
# What each voice is worth, and how much of the base size their agreement buys. With nothing on the
# shelf yet, the only voice is the deciding analyst, so the score is its own confidence. Add a
# weight beside each analyst as you declare its sources.
weights = { technical_analyst = 2.0 }
min_score = 0.4                    # below this the desk does not trade, whatever the gates said
# max_leverage = 3.0               # notional against [run].equity; a ceiling, never a target

[[conviction.ladder]]
at_least = 0.75                    # everything agrees: the whole base size
size_multiple = 1.0

[[conviction.ladder]]
at_least = 0.55
size_multiple = 0.5

[[conviction.ladder]]
at_least = 0.4
size_multiple = 0.25

[targets]
# What a trade has to be worth and how often you will take one. Yours to change; these are a start.
# The multiple is the one to think about: the cost is certain and the edge is not, so a trade worth
# a little more than it costs is a trade that needs everything to go right.
min_net_bps = 5
min_edge_multiple = 2.0
max_round_trips_per_day = 6
# max_research_bps = 25            # model spend since the last intent, against this notional;
#                                  # needs [agents.pricing.<model>] to mean anything

[trigger]
# What has to have happened before the roster is paid to have an opinion. All of it is computed from
# the snapshot and the cycles on disk, so a cycle that holds here costs one data pull and no tokens.
min_move_bps = 15
max_spread_bps = 8
cooldown_s = 900
widen_after = 6

[audit]
# The two auditors wake differently because they answer different questions. Something breaking is
# an event, so the technical auditor wakes on an alarm - a provider that stopped answering, a data
# chain that ran out, a venue that refused - and reports without acting. Whether the desk is earning
# what it costs is a stretch of time, so the quant auditor runs on a count of cycles.
on_alarm = true
alarm_cooldown_s = 1800            # per kind of alarm, not overall
quant_every_cycles = 20

# What this desk watches. Left out, every cycle is [run].instrument. Declared, a pass looks at each
# instrument for one data pull and no tokens, applies [trigger] to each, and spends its cycles on
# whatever moved most against its own spread.
# [universe]
# instruments = ["BTCUSDT", "ETHUSDT"]
# max_per_cycle = 1
# max_intents_per_day = 6          # across every instrument, not per one
# screener = false                 # ask the screener to narrow what the trigger admitted

[execution]
# sandbox keeps the intent here. signals writes it for your own executor. venue sends it, and does
# not start until `tra mandate > MANDATE.md`, a signature below, the ladder at CANARY, [canary]
# declared and the keys in your environment. `tra check` names whatever is missing.
mode = "sandbox"
# acknowledged_by = "your name"
# acknowledged_on = "YYYY-MM-DD"
# max_notional = 200

"""

FIRST_HOUR = """

## Your first hour

This project was written by `tra init --profile autonomous`, so the cycle is already wired. Three
things are yours before it can decide anything:

1. **A model.** Put your key in `.env` (copy `.env.example`) and name the provider and model in
   `[agents]` in `strategy.toml`.
2. **The mechanism and the edge.** `[agents].mechanism` is the sentence the analyst reasons from,
   and `[agents].magnitude_bps` is the edge you claim, in basis points. The cost gate holds that
   claim against what a round trip actually costs, so an honest number here is the whole point.
3. **Data to decide on.** `tra data get BTCUSDT 2025-01-01` for a dataset, and `data.toml` already
   declares the live chains a cycle pulls through.

Then `tra check` tells you what is still missing, and `tra run --env-file .env` runs one cycle.

Optional, and worth doing in this order: fill in a file under `sources/` and its analyst joins the
roster; give that analyst a weight in `[conviction].weights` and its agreement starts changing the
size; name it in `[confluence].required` and its disagreement starts refusing the trade.

`docs/boundaries.md` says what this software will not do, and why.
"""


@dataclass
class Schedule:
    """When the next cycle runs, and what ends the run.

    A desk does not want a cycle every few seconds; it wants one when a bar has closed and the
    figures have changed. So the wait lands on the interval's own boundary, and everything that
    should end a run - the budget, the kill switch, the count asked for, a keyboard - ends it by
    name rather than by the process stopping quietly.
    """

    every_s: float | None = None
    cycles: int | None = None
    kill_switch: Path | None = None
    clock: Callable[[], float] = wall_clock
    rest: Callable[[float], None] = sleep

    @property
    def looping(self) -> bool:
        return self.every_s is not None

    def stop_reason(self, result: Any, ran: int) -> str:
        if not self.looping:
            return "one cycle"
        # A pass that chose nothing has no result to read, and that is not a reason to stop.
        stopped = getattr(result, "stopped_by", None)
        if stopped and budget_spent(str(stopped)):
            return f"the budget: {stopped}"
        if self.kill_switch is not None and self.kill_switch.exists():
            return f"the kill switch at {self.kill_switch}"
        if self.cycles is not None and ran >= self.cycles:
            return f"the {self.cycles} pass(es) asked for"
        return ""

    def left(self) -> float:
        """Seconds to the next boundary, so a cycle reads a bar that has finished happening."""
        every = self.every_s or 0.0
        if every <= 0:
            return 0.0
        return round(every - (self.clock() % every), 3)

    def wait(self) -> float:
        """Sleep to the next boundary and say how long that was."""
        left = self.left()
        if left > 0:
            self.rest(left)
        return left


def run_cycle_command(args: argparse.Namespace) -> int:
    """One cycle, end to end, from the files in the project."""
    from trading_research_agents.agents.cycle import CycleConfig
    from trading_research_agents.data.sources import load_data_config
    from trading_research_agents.execution.targets import reward_risk
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        RateWindow,
        UsageLedger,
    )

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a cycle needs [strategy].agent and [agents] in strategy.toml")
    a = cfg.agents
    magnitude = a.magnitude_bps
    if magnitude is None:
        raise ConfigError("the claimed edge: [agents].magnitude_bps in strategy.toml")
    data_cfg = load_data_config(args.data_config)
    chains = {declared.kind: data_cfg.chain(declared.kind) for declared in data_cfg.kinds}
    instrument = args.instrument or cfg.run.instrument
    out = args.out or Path("runs") / "cycles"
    beside = args.config.resolve().parent
    overrides = {
        name: path
        for name, path in (
            ("macro", args.macro),
            ("onchain", args.onchain),
            ("sentiment", args.sentiment),
            ("derivatives", args.derivatives),
            ("news", args.news),
        )
        if path is not None
    }
    declared = declared_in(beside, overrides=overrides)
    series = {role: tuple(paths) for role, paths in declared.series.items()}
    news = tuple(declared.text)
    if not news and (beside / "events.csv").is_file():
        news = (beside / "events.csv",)
    # A declared roster wins; otherwise the roles with something to read are the ones to pay for.
    analysts = a.analysts or roster_for(declared, "technical_analyst")
    mode = args.mode or cfg.execution.mode
    # A hand for the instrument this run starts on, so a misconfigured mandate is refused before any
    # data is pulled. A pass over a universe builds another one for whichever instrument it chose:
    # one hand per instrument, because a hand carries the symbol an order goes out under.
    hand = _venue_hand(args, cfg, beside, instrument) if mode == "venue" else None
    # One order log for the whole run, so [controls].max_orders_per_minute is the desk's.
    from trading_research_agents.trading.dispatch import OrderRate

    rate = OrderRate(cfg.limits.max_orders_per_minute)
    keeper = _keeper(args, cfg, beside, out, instrument, rate) if mode == "venue" else None
    usage = UsageLedger(out / "usage.jsonl")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {model: Pricing(i, o) for model, (i, o) in a.pricing.items()}
    metered: dict[str, MeteredCompleter] = {}
    window = RateWindow()

    def completer_for(role: str) -> MeteredCompleter:
        # One budget and one rate window for the whole desk, one client per role: a role may run
        # on a cheaper model without the others paying for it.
        if role not in metered:
            settings = a.for_role(role)
            metered[role] = MeteredCompleter(
                _completer_for_role(a, role),
                role=role,
                provider=settings.provider,
                limits=limits,
                ledger=usage,
                pricing=pricing,
                window=window,
            )
        return metered[role]

    cycle_cfg = CycleConfig(
        instrument=instrument,
        envelope=load_envelope(args.envelope),
        venue_key=cfg.run.venue_key,
        chains=chains,
        completer_for=completer_for,
        usage=usage,
        root=out,
        mechanism=a.mechanism,
        size=cfg.run.size,
        size_quote=cfg.run.size_quote,
        magnitude_bps=magnitude,
        bar_interval_s=cfg.run.bar_interval_s,
        lookback_bars=a.lookback_bars,
        interval_s=cfg.run.bar_interval_s,
        settlement_interval_s=cfg.run.settlement_interval_s,
        latency_slippage_bps=cfg.run.slippage_bps,
        margin_bps=args.margin if args.margin is not None else Decimal(0),
        min_confidence=a.min_confidence,
        mode=mode,
        place=hand,
        matrix=cfg.confluence,
        conviction=cfg.conviction,
        max_position=cfg.limits.max_position,
        equity=cfg.run.equity,
        size_step=cfg.run.step_for(instrument),
        validators=a.validators,
        on_missing_validator=a.on_missing_validator,
        context_intervals=cfg.run.context_intervals,
        hold_limit=cfg.execution.hold_limit,
        timeout_bars=cfg.exit_rule.timeout_bars,
        regime=cfg.regime,
        targets=cfg.targets,
        desk=cfg.desk,
        held=_held_reader(cfg, out, mode, args, beside),
        wait_while_open=mode == "venue" and not cfg.execution.adopt_open_position,
        daily_loss_limit_bps=cfg.limits.daily_loss_limit_bps if mode == "venue" else None,
        trigger=cfg.trigger,
        forced=bool(args.now),
        max_risk_per_trade_bps=cfg.limits.max_risk_per_trade_bps,
        reward_to_risk=reward_risk(
            stop_frac=cfg.exit_rule.stop_sigma_frac,
            take_frac=cfg.exit_rule.take_profit_sigma_frac,
        ),
        stop_frac=cfg.exit_rule.stop_sigma_frac,
        take_frac=cfg.exit_rule.take_profit_sigma_frac,
        data_options={declared.kind: dict(declared.options) for declared in data_cfg.kinds},
        series=series,
        news=news,
        analysts=analysts,
    )
    schedule = Schedule(every_s=args.every, cycles=args.cycles, kill_switch=cfg.kill_switch)
    spine = _cycle_steps(cycle_cfg)
    # The auditors keep their own ledger, so what the desk spent deciding stays separable from what
    # it spent looking at itself.
    audits = Auditing(policy=cfg.audit, settings=a, config=args.config, runs=out)
    ran = 0
    cleared = CLEARED
    # What the models have already cost since this desk last wrote an intent, per instrument. A
    # refused cycle's tokens are not free: they are carried forward and charged to whatever trade
    # that instrument eventually produces.
    carried: dict[str, Decimal] = {}
    mid_cycle = False
    passes = 0
    lock = shutdown.claim(out) if mode == "venue" else None
    shutdown.journal(
        out,
        "started",
        every_s=args.every,
        cycles=args.cycles,
        mode=mode,
        instruments=list(cfg.universe.instruments) or [instrument],
    )
    try:
        with shutdown.as_interrupt():
            while True:
                if keeper is not None:
                    armed = cfg.kill_switch is not None and cfg.kill_switch.exists()
                    kept = keeper.run(kill=armed)
                    if kept and not args.json:
                        sys.stdout.write("\n".join(kept) + "\n")
                        sys.stdout.flush()
                    if armed:
                        stop = f"the kill switch at {cfg.kill_switch}, after flattening"
                        emit(
                            {"cycles": ran, "passes": passes, "stopped_by": stop},
                            args.json,
                            [f"scheduler: stopped by {stop}"],
                        )
                        shutdown.journal(out, "stopped", stopped_by=stop, cycles=ran)
                        return cleared
                refreshed = _refresh_sources(declared.classes.values())
                if refreshed and not args.json:
                    sys.stdout.write("\n".join(refreshed) + "\n")
                    sys.stdout.flush()
                chosen, screened = _sweep(
                    cfg, cycle_cfg, args, chains=chains, out=out, ask=completer_for
                )
                if screened is not None:
                    found, said = _screen_report(screened)
                    emit(found, args.json, said, partial(_screen_view, found))
                result: Any = None
                stop = ""
                for instrument in chosen:
                    before = usage.cost
                    held = carried.get(instrument, Decimal(0))
                    sends = (
                        _venue_hand(args, cfg, beside, instrument, rate)
                        if mode == "venue"
                        else None
                    )
                    mid_cycle = True
                    result = _one_cycle(
                        replace(
                            cycle_cfg,
                            instrument=instrument,
                            size_step=cfg.run.step_for(instrument),
                            carried_spend=held,
                            place=sends,
                            account=None if sends is None else sends.account,
                        ),
                        usage,
                        spine,
                        args,
                    )
                    mid_cycle = False
                    carried[instrument] = (
                        Decimal(0) if result.acted else held + (usage.cost - before)
                    )
                    payload, lines = _cycle_report(result, usage, mode)
                    emit(payload, args.json, lines, partial(_cycle_view, payload))
                    ran += 1
                    audits.after(payload, ran, as_json=args.json)
                    cleared = CLEARED if result.stopped_by is None else REFUSED
                    # Inside a pass only the budget and the kill switch end the run.
                    stop = schedule.stop_reason(result, 0)
                    if stop:
                        break
                # A pass counts whether or not the trigger let a cycle through: on a quiet market
                # `--cycles 6` would otherwise run for as long as the market stays quiet.
                passes += 1
                if not stop:
                    stop = schedule.stop_reason(result, passes)
                if stop:
                    if keeper is not None and stop.startswith("the kill switch"):
                        # The switch exists to get flat, not only to stop deciding.
                        kept = keeper.run(kill=True)
                        if kept and not args.json:
                            sys.stdout.write("\n".join(kept) + "\n")
                    if schedule.looping:
                        emit(
                            {"cycles": ran, "passes": passes, "stopped_by": stop},
                            args.json,
                            [
                                f"scheduler: stopped by {stop}, after {passes} pass(es) and "
                                f"{ran} cycle(s)"
                            ],
                        )
                    shutdown.journal(out, "stopped", stopped_by=stop, cycles=ran, passes=passes)
                    return cleared
                # Said before the wait, not after it: an operator looking at the screen in the
                # meantime needs to know the desk is waiting rather than stuck.
                left = schedule.left()
                if not args.json:
                    sys.stdout.write(f"scheduler: next cycle in {left}s\n")
                    sys.stdout.flush()
                if _watching(args.json) and left > 0:
                    _ui().countdown(left)
                else:
                    schedule.rest(left)
    except KeyboardInterrupt as exc:
        # Ctrl-C, a service manager's SIGTERM, a dropped SSH session: one way out, said by name and
        # written down. Whatever a cycle had already put on disk stays there.
        why = shutdown.stopped_by(exc)
        if mid_cycle:
            why = f"{why}, in the middle of a cycle"
        emit(
            {"cycles": ran, "stopped_by": why},
            args.json,
            [f"scheduler: stopped by {why}, after {ran} cycle(s)"],
        )
        shutdown.journal(out, "stopped", stopped_by=why, cycles=ran)
        # Stopping between cycles is the ordinary way a desk ends; stopping inside one is not.
        return REFUSED if mid_cycle else cleared
    finally:
        if lock is not None:
            shutdown.release(lock)


@dataclass
class Auditing:
    """Waking the auditors between cycles: one on an alarm, one on a count.

    Something breaking is an event and belongs to the technical auditor; whether the desk is earning
    what it costs is a stretch of time and belongs to the quant auditor. Neither can act on what it
    finds, and neither is allowed to end a run: an auditor that fails is a line in the report.
    """

    policy: Any
    settings: Any
    config: Path
    runs: Path
    watch: Any = None
    clock: Callable[[], float] = wall_clock

    def __post_init__(self) -> None:
        from trading_research_agents.agents.alarms import Watch

        if self.watch is None:
            self.watch = Watch(cooldown_s=self.policy.alarm_cooldown_s)

    def due(self, record: Mapping[str, Any], ran: int) -> tuple[tuple[str, ...], tuple[Any, ...]]:
        """Which auditors to ask after this cycle, and the alarms that asked for one."""
        from trading_research_agents.agents.alarms import alarms_of

        who: list[str] = []
        fired: tuple[Any, ...] = ()
        if self.policy.on_alarm:
            fired = self.watch.due(alarms_of(record), self.clock())
            if fired:
                who.append("tech_auditor")
        every = self.policy.quant_every_cycles
        if every is not None and ran % every == 0:
            who.append("quant_auditor")
        return tuple(who), fired

    def after(self, record: Mapping[str, Any], ran: int, *, as_json: bool = False) -> list[str]:
        """Ask whoever is due, and say what they found. Never raises into the run."""
        who, fired = self.due(record, ran)
        if not who:
            return []
        from trading_research_agents.agents.audit import AuditConfig, run_audit
        from trading_research_agents.llm.usage import UsageLedger

        out = self.runs.parent / "audits"
        said = [
            f"audit: {', '.join(who)} asked"
            + (f" on {', '.join(a.kind for a in fired)}" if fired else f" after {ran} cycle(s)")
        ]
        for alarm in fired:
            said.append(f"  alarm {alarm.kind}: {alarm.detail}")
        try:
            usage = UsageLedger(out / "usage.jsonl")
            audit = AuditConfig(
                runs=self.runs,
                completer_for=_metered_roles(self.settings, usage, floor=REPORT_HEADROOM),
                usage=usage,
                root=out,
                config=self.config,
                auditors=who,
            )
            if not as_json and _watching():
                with _ui().console().status(f"asking {', '.join(who)}", spinner="line"):
                    result = run_audit(audit)
            else:
                result = run_audit(audit)
        except Exception as exc:
            # A failing auditor is a thing to report, never a thing that ends a run.
            said.append(f"  the audit could not run: {exc.__class__.__name__}: {exc}")
            if not as_json:
                print(plain_text("\n".join(said)), flush=True)
            return said
        said.extend(f"  {line}" for line in _audit_lines(result, []))
        if not as_json:
            print(plain_text("\n".join(said)), flush=True)
        return said


def _sweep(
    cfg: Any,
    cycle_cfg: Any,
    args: argparse.Namespace,
    *,
    chains: Mapping[str, Any],
    out: Path,
    ask: Callable[[str], Any],
) -> tuple[tuple[str, ...], Any]:
    """Which instruments this pass runs a cycle on, and the screen that chose them.

    A desk that watches one instrument runs it, as it always did. A desk that declared a universe
    takes the cheap look first - one data pull each, the same trigger applied per instrument - and
    spends its cycles on what that admitted.
    """
    from trading_research_agents.agents.screen import asking, sweep
    from trading_research_agents.execution.trigger import Trigger
    from trading_research_agents.store.snapshot import cycle_id

    universe = cfg.universe
    if args.instrument or universe.idle:
        return (cycle_cfg.instrument,), None
    now = cycle_cfg.clock()
    # --now overrides the trigger for the cycle, so it overrides it for the pass that feeds the
    # cycle too: an operator who asked anyway should not be told nothing was worth looking at.
    trigger = Trigger() if args.now else cfg.trigger
    screener = None
    if universe.screener:
        screener = asking(ask("market_screener"), mechanism=cycle_cfg.mechanism, at=cycle_id(now))

    open_now: set[str] = set()
    if cfg.execution.places_orders and not cfg.execution.adopt_open_position:
        reader = cycle_cfg.held
        if reader is not None:
            try:
                rows, _source = reader()
                open_now = {row.instrument for row in rows}
            except Exception:
                # The cycle reads the book again and refuses on its own if it cannot; the pass only
                # uses this to avoid asking the roster about something already open.
                open_now = set()

    cap = cfg.desk.max_open_positions
    full = (
        f"the desk holds {len(open_now)} position(s), its declared [desk].max_open_positions"
        if cap is not None and open_now and len(open_now) >= cap
        else ""
    )

    def look(watch: Any) -> Any:
        return sweep(
            universe,
            chains,
            trigger=trigger,
            root=out,
            now=now,
            max_round_trips_per_day=cfg.targets.max_round_trips_per_day,
            options=cycle_cfg.data_options,
            ask=screener,
            desk=cfg.desk,
            held=cycle_cfg.held,
            watch=watch,
            open_now=open_now,
            full=full,
        )

    if args.json or not _watching():
        screened = look(lambda _step: None)
    else:
        # A pass pulls one snapshot per instrument before any cycle starts; on a slow venue that
        # is seconds per name, and a blank terminal for that long reads as a hung process.
        steps = [*universe.instruments, *(["market_screener"] if universe.screener else [])]
        with _ui().CycleView(cycle_cfg.usage, steps, title="screen") as view:
            screened = look(view.on_step)
    where = out.parent / "screens"
    where.mkdir(parents=True, exist_ok=True)
    write_json(where / f"{screened.at}.json", screened.record())
    return screened.chosen, screened


def _screen_report(screened: Any) -> tuple[dict[str, Any], list[str]]:
    from trading_research_agents.execution.universe import why_no_cycle

    """One pass over the universe as a record and as the lines a person reads."""
    payload = screened.record()
    admitted = sum(1 for c in screened.candidates if c.taken)
    lines = [
        f"screen {screened.at}: {len(screened.candidates)} instrument(s) looked at, "
        f"{admitted} admitted, {len(screened.chosen)} taken"
    ]
    for candidate in screened.candidates:
        state = (
            "taken"
            if candidate.instrument in screened.chosen
            else ("admitted" if candidate.taken else "held")
        )
        lines.append(
            f"  {candidate.instrument:<12} {state:<9} "
            f"moved {candidate.moved_bps if candidate.moved_bps is not None else '-'} bps, "
            f"spread {candidate.spread_bps if candidate.spread_bps is not None else '-'} bps, "
            f"pressure {candidate.pressure if candidate.pressure is not None else '-'}"
        )
    for refusal in screened.refusals[:8]:
        lines.append(f"  held {refusal}")
    if not screened.chosen:
        lines.append(
            f"no cycle this pass: {why_no_cycle(payload.get('candidates') or [])}"
            + (
                "; only the screener was asked"
                if screened.asked_screener
                else "; no model was asked"
            )
        )
    return payload, lines


def _screen_view(payload: Mapping[str, Any]) -> Any:
    return _ui().screen_view(payload)


def _keeper(
    args: argparse.Namespace, cfg: Any, project: Path, out: Path, instrument: str, rate: Any
) -> Any:
    """What looks after positions between decisions: time exits, venue exits, the kill switch."""
    from trading_research_agents.trading.positions import Keeper

    hands: dict[str, Any] = {}

    def hand_for(name: str) -> Any:
        if name not in hands:
            hands[name] = _venue_hand(args, cfg, project, name, rate)
        return hands[name]

    return Keeper(
        root=out,
        hand_for=hand_for,
        instruments=tuple(cfg.universe.instruments) or (instrument,),
        bar_interval_s=cfg.run.bar_interval_s,
        timeout_bars=cfg.exit_rule.timeout_bars,
        hold_limit=cfg.execution.hold_limit,
    )


def _held_reader(cfg: Any, out: Path, mode: str, args: argparse.Namespace, project: Path) -> Any:
    """Where a cycle looks to find out what this desk is already carrying.

    In venue mode the venue is the book and nothing else is: it knows what filled, and what the
    attached exits closed while nobody was watching. Everywhere else the intents on disk are all
    there is, and the verdict says so rather than implying a venue was asked.
    """
    from trading_research_agents.execution.desk import Open, filled_from_records, from_records

    if cfg.desk.idle:
        return None
    if mode != "venue":
        return lambda: from_records(out, at=datetime.now(UTC))

    def ask_the_venue() -> tuple[tuple[Open, ...], str]:
        ours = filled_from_records(out)
        names = cfg.universe.instruments or (args.instrument or cfg.run.instrument,)
        held: list[Open] = []
        for name in names:
            size = _venue_hand(args, cfg, project, name).position()
            if not size:
                continue
            # The venue says the position is there; this desk's own intent says what it has at
            # risk, which the venue has no way to know.
            mine = ours.get(name)
            side = "buy" if size > 0 else "sell"
            if mine is not None and mine.side and mine.side != side:
                # The venue holds the other side from what this desk filled last: not ours to state.
                mine = None
            held.append(
                Open(
                    instrument=name,
                    side=side,
                    risk_bps=None if mine is None else mine.risk_bps,
                    notional=None if mine is None else mine.notional,
                )
            )
        return tuple(held), "the venue, with what each one risks from this desk's own intents"

    return ask_the_venue


def _cycle_steps(cfg: Any) -> list[str]:
    """The pipeline this project configured, in order, so the live table starts complete.

    An operator watching a cycle should see every step that can happen, including the ones that are
    not configured - a table that grows as it goes tells you less than one that was there first.
    """
    steps = ["data"]
    if not cfg.trigger.idle:
        steps.append("trigger")
    if cfg.size_quote is not None:
        steps.append("size")
    if cfg.account is not None:
        steps.append("capital")
    steps.extend(["regime", "technical"])
    # Every reading analyst has a row, asked or not; the row says which.
    steps.extend(["macro_analyst", "onchain_analyst", "sentiment_analyst"])
    steps.append("cost")
    steps.extend(f"validator_{stance}" for stance in cfg.validators)
    if not cfg.matrix.idle:
        steps.append("confluence")
    if not cfg.conviction.idle:
        steps.append("conviction")
    if cfg.max_risk_per_trade_bps is not None:
        steps.append("risk")
    if cfg.desk.bounds_a_trade:
        steps.append("desk")
    steps.append("cost gate")
    if not cfg.targets.idle:
        steps.append("targets")
    steps.append("intent")
    if cfg.mode == "venue":
        steps.append("venue")
    return steps


def _one_cycle(cfg: Any, usage: Any, spine: list[str], args: argparse.Namespace) -> Any:
    """One cycle, with the steps drawn as they happen when a person is watching."""
    from trading_research_agents.agents.cycle import run_cycle

    if args.json or not _watching():
        return run_cycle(cfg)
    with _ui().CycleView(usage, spine) as view:
        return run_cycle(replace(cfg, watch=view.on_step))


def order_outcome(placed: Mapping[str, Any]) -> str:
    """What became of a sent order, in one phrase, from the record the venue hand wrote."""
    if placed.get("interrupted"):
        return str(placed.get("note") or "interrupted")
    filled = placed.get("filled")
    if placed.get("note"):
        return str(placed["note"])
    if filled not in (None, "", "0", "0.0"):
        return f"filled {filled}" + (f" at {placed['average']}" if placed.get("average") else "")
    return str(placed.get("status") or "sent; the venue reported no state")


def _cycle_view(payload: Mapping[str, Any]) -> Any:
    return _ui().cycle_view(payload)


def _cycle_report(result: Any, usage: Any, mode: str) -> tuple[dict[str, Any], list[str]]:
    """One cycle as a record and as the lines a person reads."""
    payload = result.record()
    payload["tokens"] = usage.tokens
    payload["out"] = str(result.path)
    spent = f"{result.fan.seconds}s of agents"
    if result.debate is not None:
        spent += f", {result.debate.seconds}s of validators"
    lines = [f"cycle {result.cycle} {result.instrument}: {spent}"]
    data = result.briefs.get("data") or {}
    for kind, value in sorted((data.get("values") or {}).items()):
        lines.append(f"  {kind:<14} {value['source']:<18} age {value['age_s']}s")
    for row in result.briefs.get("series") or []:
        lines.append(
            f"  {row['series']:<14} {row['value']} as of {str(row['observed_at'])[:16]}, "
            f"{row['age_h']}h old, {row['state']}; {row['origin']}"
        )
    for role, why in sorted((result.briefs.get("not_asked") or {}).items()):
        lines.append(f"{role.replace('_analyst', ''):<10} {why.removeprefix(role + ': ')}")
    for role, said in sorted(result.context.items()):
        if role.startswith("validator"):
            continue
        state = said.get("regime") or said.get("signal") or said.get("state") or said.get("error")
        lines.append(f"{role.replace('_analyst', ''):<10} {state}: {said.get('reason', '')}")
    if result.analyst is not None:
        lines.append(
            f"technical: {result.analyst.target} on {result.analyst.setup} at "
            f"{result.analyst.confidence:.2f} over {result.analyst.horizon_bars} bars: "
            f"{result.analyst.reason}"
        )
        if result.cited:
            read = ", ".join(f"{f['figure']}={f['value']}" for f in result.cited)
            lines.append(f"read: {read}")
    if result.wake is not None and not result.wake.ask:
        lines.append(f"held: {result.wake.reasons[0]}")
        if result.wake.moved_bps is not None:
            lines.append(
                f"the mid moved {result.wake.moved_bps} bps against the "
                f"{result.wake.required_bps} bps this desk asks for before it looks"
            )
    for role, said in sorted(result.context.items()):
        if not role.startswith("validator"):
            continue
        verdict = said.get("verdict") or f"failed: {said.get('error', '')}"
        cited = len(said.get("claims") or ())
        dropped_claims = len(said.get("discarded") or ())
        counted = f"{cited} cited figure(s)"
        if dropped_claims:
            counted += f", {dropped_claims} discarded"
        lines.append(f"{role.replace('_', ' '):<18} {verdict} on {counted}")
    if result.confluence is not None:
        agreement = result.confluence
        states = ", ".join(
            f"{role.replace('_analyst', '')} {state}"
            for role, state in sorted(agreement.states.items())
        )
        counted = f"support {agreement.support}, opposition {agreement.opposition}"
        lines.append(f"confluence: {states or 'no role required'}; {counted}")
    if result.stake is not None:
        stake = result.stake
        score = stake.score
        most = dict(score.weights)
        voices = ", ".join(
            f"{role.replace('_analyst', '')} {value}"
            + (f" of {most[role]}" if role in most else "")
            for role, value in score.contributions
        )
        lines.append(f"conviction: {score.score} of 1.0 ({voices})")
        if stake.admitted:
            leverage = "" if stake.leverage is None else f", {stake.leverage}x equity"
            lines.append(f"stake: {stake.size} at {stake.multiple} of the base size{leverage}")
    if result.risk is not None and result.risk.risk_bps is not None:
        risk = result.risk
        lines.append(
            f"risk: {risk.risk_bps} bps of equity at the stop against the declared "
            f"{risk.allowed_bps} bps, size {risk.size}"
        )
    if result.exits:
        shown = result.exits
        said = [f"exits: {shown['side']} at about {shown['entry']}"]
        for label, name in (("take profit", "take"), ("stop loss", "stop")):
            if f"{name}_price" in shown:
                pnl = shown.get(f"{name}_pnl_bps", shown[f"{name}_bps"])
                said.append(f"{label} {shown[f'{name}_price']} (position {pnl} bps)")
        lines.append(", ".join(said))
    if result.targets is not None:
        met = result.targets
        lines.append(
            f"targets: net {met.net_bps.quantize(Decimal('0.01'))} bps, "
            f"reward-to-risk {met.reward_risk}, {met.round_trips_today} round trip(s) today"
        )
        if met.edge_multiple is not None:
            lines.append(f"edge over cost: {met.edge_multiple}x the round trip")
        if met.research_bps is not None:
            lines.append(
                f"research: {met.research_bps} bps of this trade's notional in model calls "
                "since the last intent"
            )
    if result.verdict is not None:
        cost = result.verdict.cost
        total = cost.round_trip_bps.quantize(Decimal("0.01"))
        guessed = cost.estimated_bps.quantize(Decimal("0.01"))
        lines.append(f"cost: {total} bps round trip, {guessed} of it estimated")
        lines.append(f"gate: {result.verdict.reason}")
        lines.append(
            f"the edge is {result.verdict.edge_certainty.value}: {result.verdict.edge_source}"
        )
        for component in cost.components:
            row = component.record()
            lines.append(f"  {row['name']:<18} {row['bps']:>10} bps  {row['certainty']}")
    if result.intent is not None:
        lines.append(f"intent: {result.intent['target']} {result.intent['size']} ({mode})")
        placed = result.placed or {}
        if placed.get("sent"):
            at = "" if not placed.get("average") else f" at {placed['average']}"
            lines.append(
                f"placed: {placed.get('side')} {placed.get('size')} on {placed.get('venue')}"
                f"{at}, stop {placed.get('stop_price')}, take {placed.get('take_price')}"
            )
            # Sent is not filled. What became of the order is the line an operator acts on.
            lines.append(f"  outcome: {order_outcome(placed)}")
        elif placed.get("refused"):
            lines.append(f"not placed: {placed['refused']}")
    for refusal in result.refusals[:6]:
        lines.append(f"  refused {refusal}")
    if result.stopped_by:
        lines.append(f"stopped by: {result.stopped_by}")
    lines.append(f"snapshot.json, briefs, answers and cycle.json in {result.path}")
    return payload, lines


def check_project(args: argparse.Namespace) -> int:
    """What a first run needs, checked in the order it would fail, said in plain words."""
    good, bad, next_command = _check(args)
    lines = [*(f"ok    {g}" for g in good), *(f"todo  {b}" for b in bad)]
    lines.append(
        f"ready to run `{next_command}`" if not bad else f"{len(bad)} thing(s) to fix first"
    )
    emit({"ok": good, "todo": bad}, args.json, lines, lambda: _ui().check_table(good, bad))
    return CLEARED if not bad else REFUSED


def _check(args: argparse.Namespace) -> tuple[list[str], list[str], str]:
    good: list[str] = []
    bad: list[str] = []
    beside = args.config.resolve().parent
    env = _environment(args.env_file, beside=beside)
    read_from = _env_file_used(args.env_file, beside)
    if read_from is not None:
        good.append(f"keys: read from {read_from.name} beside the config, and from the environment")
    cfg = None
    try:
        cfg = load_strategy_config(args.config)
        good.append(
            f"{args.config}: valid; core {cfg.strategy.name!r}; instrument {cfg.run.instrument}"
        )
    except (ConfigError, OSError, ValueError) as exc:
        bad.append(f"{args.config}: {exc}")
    channels: tuple[Any, ...] = ()
    try:
        envelope = load_envelope(args.envelope)
        channels = tuple(envelope.channels)
        good.append(f"{args.envelope}: {len(envelope.channels)} channels")
        if cfg is not None and not any(c.key == cfg.run.venue_key for c in envelope.channels):
            bad.append(f"[run].venue_key {cfg.run.venue_key!r} is not a channel in {args.envelope}")
    except (EnvelopeError, OSError, ValueError) as exc:
        bad.append(f"{args.envelope}: {exc}")
    if cfg is not None:
        # An autonomous project decides on live chains, so a recorded dataset and a funding file
        # are what a backtest needs, not what a cycle needs. Saying otherwise sends an operator to
        # fetch a day of prints they will not use.
        cycles = cfg.agents is not None
        found = _datasets(args.config)
        if cfg.run.instrument in found:
            good.append(f"dataset: {found[cfg.run.instrument]}")
        elif cycles:
            good.append(
                f"dataset: none; `tra run` decides on the live chains, and `tra backtest` is the "
                f"one that would need `tra data get {cfg.run.instrument} YYYY-MM-DD`"
            )
        else:
            bad.append(
                f"no dataset for {cfg.run.instrument} in {_data_dir(args.config)}: "
                f"run `tra data get {cfg.run.instrument} YYYY-MM-DD`"
            )
        funding = _data_dir(args.config) / f"{cfg.run.instrument}-funding.json"
        if cfg.funding is not None or funding.is_file():
            good.append("funding: present")
        elif cycles and _declares_kind(args.config, "funding"):
            good.append("funding: live, from the chain in data.toml")
        else:
            bad.append(
                "funding: none; a run that spans a settlement will refuse (tra data get fetches it)"
            )
        observation = args.config.resolve().parent / "observation.toml"
        if observation.is_file():
            from trading_research_agents.chain.observation import ObservationError, load_observation

            try:
                declared = load_observation(observation)
            except ObservationError as exc:
                bad.append(f"observation.toml: {exc}")
            else:
                good.append(
                    f"observation.toml: {declared.subject!r}; `tra chain observe` before any memo"
                )
        shelf = declared_in(args.config.resolve().parent)
        for reading in inspect_shelf(shelf):
            where = f"sources/{reading.name}.toml"
            if reading.error:
                bad.append(f"{where}: {reading.error}")
            elif reading.missing:
                bad.append(f"{where}: declared but not on disk: {', '.join(reading.missing)}")
            else:
                listed = ", ".join(reading.entries)
                good.append(f"{where}: {len(reading.entries)} declared ({listed})")
                one_sided = _one_sided(args.config.resolve().parent / where)
                if one_sided:
                    good.append(
                        f"{where}: {', '.join(one_sided)} declare one side of the threshold "
                        "only, so they count as a full +1 or -1 whenever crossed; declare a "
                        "bullish and a bearish line and the score grades how far past it they sit"
                    )
        fresh, stale = _source_lines(
            path for name, path in shelf.classes.items() if name not in TEXT_CLASSES
        )
        good.extend(fresh)
        bad.extend(stale)
        if shelf.empty:
            good.append(
                f"sources: {', '.join(shelf.empty)} on the shelf, nothing declared yet; "
                "fill one in and its analyst joins the roster"
            )
        roster = roster_for(shelf, "technical_analyst")
        good.append(f"roster: {', '.join(roster)}; `tra run` asks these")
        ready, missing = _gate_lines(
            cfg,
            roster,
            project=args.config.resolve().parent,
            tier=Ladder(args.ladder).tier.name,
            env=env,
            entry_role=next(
                (c.entry_role for c in channels if c.key == cfg.run.venue_key), "undeclared"
            ),
        )
        good.extend(ready)
        bad.extend(missing)
        data_config = args.config.resolve().parent / "data.toml"
        if data_config.is_file():
            from trading_research_agents.data.sources import DataConfigError, load_data_config

            try:
                declared_data = load_data_config(data_config)
            except DataConfigError as exc:
                bad.append(f"data.toml: {exc}")
            else:
                kinds = ", ".join(k.kind for k in declared_data.kinds)
                good.append(f"data.toml: chains for {kinds}; `tra run` uses them")
                if not any(k.kind == "orderbook" for k in declared_data.kinds):
                    bad.append(
                        "data.toml: no [data.orderbook] chain, so a cycle cannot price a trade "
                        "and `tra run` will stop at the cost gate"
                    )
                good.extend(_agrees_across_files(cfg, declared_data, bad))
        elif cfg.agents is not None:
            bad.append("data.toml: missing; `tra run` needs it (tra init writes one)")
        if cfg.agents is not None and cfg.agents.provider == PLACEHOLDER_PROVIDER:
            bad.append(
                "[agents].provider is still the placeholder; name the provider you have a key "
                f"for, and its model ({', '.join(providers())})"
            )
        elif cfg.agents is not None:
            try:
                p = provider(cfg.agents.provider)
            except UnknownProviderError as exc:
                bad.append(str(exc))
            else:
                if p.key_env and not env.get(p.key_env):
                    bad.append(f"{p.key_env} is not set; put it in .env and pass --env-file .env")
                elif not p.installed:
                    module, extra = p.needs
                    bad.append(
                        f"provider {p.name}: the {module} SDK is not installed; "
                        f"pip install 'trading-research-agents[{extra}]'"
                    )
                else:
                    good.append(f"provider {p.name}: ready")
        if cfg.live is not None:
            tier = Ladder(args.ladder).tier
            if tier is Tier.CANARY:
                good.append(f"ladder: {tier.name}; live sessions may run")
            else:
                bad.append(f"ladder: {tier.name}; `tra live` needs CANARY (promote with evidence)")
            missing = [k for k in live_venue(cfg.live.venue).env_keys if not env.get(k)]
            if missing:
                bad.append(f"missing {', '.join(missing)} for the live venue")
    next_command = "tra backtest"
    if not args.config.is_file():
        next_command = "tra init my-run"
    elif cfg is not None and cfg.agents is not None:
        next_command = "tra run"
    elif cfg is not None and cfg.run.instrument not in _datasets(args.config):
        next_command = f"tra data get {cfg.run.instrument} YYYY-MM-DD"
    return good, bad, next_command


def _venue_hand(
    args: argparse.Namespace, cfg: Any, project: Path, instrument: str = "", rate: Any = None
) -> Any:
    """What venue mode sends with, built only after everything standing in the way is cleared."""
    from trading_research_agents.execution.mandate import blocking
    from trading_research_agents.trading.dispatch import credentials_for, hand_for

    mandate = cfg.execution
    env = _environment(getattr(args, "env_file", None))
    keys = tuple(credentials_for(mandate.venue).values())
    stops = blocking(
        mandate,
        project=project,
        tier=Ladder(args.ladder).tier.name,
        missing_keys=[k for k in keys if not env.get(k)],
        canary_declared=cfg.canary is not None,
    )
    if stops:
        raise ConfigError("venue mode is not authorised here: " + "; ".join(stops))
    channel = next(
        (c for c in load_envelope(args.envelope).channels if c.key == cfg.run.venue_key), None
    )
    if channel is None:
        raise ConfigError(f"[run].venue_key {cfg.run.venue_key!r} is not a channel in the envelope")
    if channel.entry_role not in ("maker", "taker"):
        raise ConfigError(
            f"channel {channel.key!r} does not say whether entry is maker or taker, and the fee "
            "you priced depends on it; name it <entry>-<exit>, as in maker-taker"
        )
    return hand_for(
        venue=mandate.venue,
        symbol=_symbol_for(cfg, project, instrument),
        instrument=instrument or cfg.run.instrument,
        max_notional=mandate.max_notional,
        role=channel.entry_role,
        exits=mandate.exits,
        environment=cfg.live.environment if cfg.live is not None else "demo",
        limit_timeout_s=mandate.limit_timeout_s,
        price_offset_bps=mandate.price_offset_bps,
        adopt_open_position=mandate.adopt_open_position,
        params=mandate.params,
        max_drift_bps=mandate.max_drift_bps,
        leverage=mandate.leverage,
        margin_mode=mandate.margin_mode,
        max_slippage_bps=mandate.max_slippage_bps,
        max_margin_share=mandate.max_margin_share,
        liquidation_buffer_bps=mandate.liquidation_buffer_bps,
        rate=rate,
        env=env,
    )


def _symbol_for(cfg: Any, project: Path, instrument: str = "") -> str:
    """The venue's own name for one instrument, as data.toml already maps it."""
    named = instrument or cfg.run.instrument
    declared = project / "data.toml"
    if declared.is_file():
        raw = tomllib.loads(declared.read_text(encoding="utf-8"))
        mapped = (raw.get("data", {}).get("symbols") or {}).get(named)
        if mapped:
            return str(mapped)
    return str(named)


def _agrees_across_files(cfg: Any, data: Any, bad: list[str]) -> list[str]:
    """Whether the instrument and the venue mean the same thing in every file that names them.

    Three declarations sit in three files, and a desk that prices one venue's fees while taking its
    candles from another has made a decision without noticing. Nothing here decides for them; it
    says what the files add up to.
    """
    good: list[str] = []
    instrument = cfg.run.instrument
    venue = cfg.run.venue_key.partition("/")[0]
    symbols = dict(data.settings.get("symbols") or {})
    if instrument in symbols:
        good.append(f"symbols: {instrument} is {symbols[instrument]} to the sources")
    else:
        good.append(
            f"symbols: {instrument} is not mapped in data.toml, so each source looks it up itself"
        )
    named = {
        source.partition(":")[2]
        for kind in data.kinds
        for source in kind.chain
        if source.startswith("ccxt:")
    }
    strangers = sorted(named - {venue})
    if strangers:
        elsewhere = ", ".join(strangers)
        good.append(
            f"venues: fees are priced on {venue}, data also comes from {elsewhere}; "
            "deliberate is fine, accidental is not"
        )
    if cfg.execution.places_orders and cfg.execution.venue != venue:
        bad.append(
            f"[execution].venue is {cfg.execution.venue!r} but [run].venue_key prices {venue!r}; "
            "an order would go to one venue at the other's fee"
        )
    return good


def _declares_kind(config: Path, kind: str) -> bool:
    """Whether data.toml beside this config declares a chain for one kind of live figure."""
    where = config.resolve().parent / "data.toml"
    if not where.is_file():
        return False
    try:
        raw = tomllib.loads(where.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    table = raw.get("data")
    return isinstance(table, dict) and isinstance(table.get(kind), dict)


def _gate_lines(
    cfg: Any,
    roster: tuple[str, ...],
    *,
    project: Path,
    tier: str,
    env: Mapping[str, str],
    entry_role: str = "undeclared",
) -> tuple[list[str], list[str]]:
    """What the two declared gates will do, and where they contradict the rest of the project.

    A gate that can never pass is worth saying out loud here, before a cycle spends a token finding
    out: a required role nobody asks, or a vote count nobody can cast.
    """
    good: list[str] = []
    bad: list[str] = []
    matrix = cfg.confluence
    stances = () if cfg.agents is None else cfg.agents.validators
    if stances:
        good.append(f"validators: {', '.join(stances)}; asked once there is a proposal")
    if matrix.idle:
        good.append("confluence: not declared; the context is recorded but gates nothing")
    else:
        good.append(
            f"confluence: needs {', '.join(matrix.required) or 'no role'}; "
            f"long {list(matrix.long_states)}, short {list(matrix.short_states)}; "
            f"a missing state means {matrix.on_missing}"
        )
        absent = sorted(set(matrix.required) - set(roster))
        if absent:
            bad.append(
                f"[confluence].required names {absent}, which the roster does not ask; "
                "declare their sources or the cycle refuses every time"
            )
        if matrix.min_support > len(stances):
            bad.append(
                f"[confluence].min_support is {matrix.min_support} but "
                f"{len(stances)} validator(s) are asked; no cycle can clear it"
            )
    from trading_research_agents.execution.mandate import blocking

    mandate = cfg.execution
    if entry_role in ("maker", "taker"):
        crosses = "crosses the book at once" if entry_role == "taker" else "rests on the book"
        good.append(
            f"channel: {cfg.run.venue_key} enters as a {entry_role}, priced at that fee; "
            f"an order {crosses}"
        )
    if not mandate.places_orders:
        good.append(
            f"execution: {mandate.mode}; nothing leaves the machine"
            if mandate.mode == "sandbox"
            else "execution: signals; intents are written for your own executor"
        )
    else:
        from trading_research_agents.trading.dispatch import credentials_for

        keys = tuple(credentials_for(mandate.venue).values())
        stops = blocking(
            mandate,
            project=project,
            tier=tier,
            missing_keys=[k for k in keys if not env.get(k)],
            canary_declared=cfg.canary is not None,
        )
        if stops:
            bad.extend(f"execution: {stop}" for stop in stops)
        else:
            good.append(
                f"execution: venue {mandate.venue}, signed by {mandate.acknowledged_by} on "
                f"{mandate.acknowledged_on}, at most {mandate.max_notional} per order"
            )
            # Two declarations that only meet at the last gate, after a cycle has been paid for.
            # The price where they stop agreeing is arithmetic, and it can be said here instead.
            if mandate.max_notional is not None:
                _size_against_cap(cfg, mandate.max_notional, good, bad)
            good.extend(_capital_notes(cfg))
            if mandate.exits == "attached":
                good.append(
                    "execution: the stop and the take profit go to the venue with the entry, so a "
                    "fill is never left unattended"
                )
            else:
                bad.append(
                    '[execution].exits is "none": a fill would sit at the venue with no stop and '
                    "no take profit, and only you would be carrying them"
                )
            if entry_role == "maker":
                good.append(
                    "execution: an order that has not filled is cancelled after "
                    f"{mandate.limit_timeout_s}s; nothing is chased across the spread"
                )
            elif entry_role == "undeclared":
                bad.append(
                    f"channel {cfg.run.venue_key!r} does not say whether entry is maker or taker; "
                    "the fee you priced depends on it"
                )
    if cfg.agents is not None and cfg.agents.fallback:
        unknown = []
        for name in cfg.agents.fallback:
            named, _ = parse_link(name)
            if named not in providers():
                unknown.append(name)
        if unknown:
            bad.append(f"[agents].fallback names {unknown}, which no provider is registered for")
        else:
            good.append(
                f"models: fallback {', '.join(cfg.agents.fallback)}; "
                "tried in order when a provider cannot be reached"
            )
    if cfg.agents is not None:
        good.extend(_load_notes(cfg.agents, roster))
    good.append(_regime_note(cfg))
    rule = cfg.conviction
    if rule.idle:
        good.append("conviction: not declared; every admitted cycle trades the base size")
    else:
        rungs = ", ".join(f"{t.at_least}+ -> {t.size_multiple}x" for t in rule.ladder)
        good.append(
            f"conviction: floor {rule.min_score}; ladder {rungs or 'none'}"
            + (
                f"; notional at most {rule.max_leverage}x the equity (a cap on exposure, not the "
                "venue's leverage, which is [execution].leverage)"
                if rule.max_leverage
                else ""
            )
        )
        good.append(
            "conviction: every voice counts as strong as it is - the deciding analyst by its "
            "confidence, a reading analyst by how far its series sit past your lines - and the "
            "score is their net over what they expressed; opposition shrinks the size, and only "
            "opposition that outweighs the case refuses the trade"
        )
        if not rule.ladder:
            bad.append(
                "[conviction] has no ladder, so no score can size a trade; "
                "declare [[conviction.ladder]] rungs or drop the table"
            )
        unweighted = sorted(set(roster) - set(rule.weights))
        if unweighted:
            good.append(
                f"conviction: {unweighted} is asked but carries no weight; "
                "it explains without moving the size"
            )
    asked = cfg.agents.validators if cfg.agents is not None else ()
    gated = cfg.confluence.min_support > 0 or cfg.confluence.max_opposition is not None
    if asked and not gated and rule.validator_weight <= 0:
        bad.append(
            f"validators: {', '.join(asked)} are asked every cycle and their votes move nothing; "
            "give them [conviction].validator_weight, or [confluence].min_support / "
            "max_opposition, or stop asking them"
        )
    targets = cfg.targets
    if targets.idle:
        good.append(
            "targets: not declared; only the cost gate stands between a claim and an intent"
        )
    else:
        said = []
        if targets.min_net_bps is not None:
            said.append(f"at least {targets.min_net_bps} bps net")
        if targets.min_edge_multiple is not None:
            said.append(f"at least {targets.min_edge_multiple}x the round trip")
        if targets.min_reward_risk is not None:
            said.append(f"reward-to-risk {targets.min_reward_risk}")
        if targets.max_research_bps is not None:
            said.append(f"at most {targets.max_research_bps} bps of notional in model calls")
        if targets.max_round_trips_per_day is not None:
            said.append(f"at most {targets.max_round_trips_per_day} round trips a day")
        good.append("targets: " + "; ".join(said))
    if targets.min_edge_multiple is None:
        good.append(
            "targets: no edge multiple declared, so a trade worth two basis points more than it "
            "costs can still pass; [targets].min_edge_multiple is what stops that"
        )
    priced = cfg.agents is not None and any(i > 0 or o > 0 for i, o in cfg.agents.pricing.values())
    if targets.max_research_bps is not None and not priced:
        bad.append(
            "[targets].max_research_bps is declared and no model price is, so the research cost "
            "reads as zero whatever the models spend; put what you were quoted in "
            "[agents.pricing.<model>] or drop the target"
        )
    good.extend(_risk_notes(cfg, bad))
    good.extend(_trigger_notes(cfg))
    good.extend(_spend_notes(cfg))
    good.extend(_audit_notes(cfg))
    good.extend(_universe_notes(cfg, bad))
    good.extend(_size_notes(cfg))
    good.extend(_desk_notes(cfg, bad))
    good.append(
        "keys: this check reads which are set, not whether they work; `tra keys --probe` asks each "
        "provider and the venue itself, sends no prompt and places no order"
    )
    return good, bad


def _refresh_sources(declarations: Iterable[Path]) -> list[str]:
    """Pull every fetched series that is due, before the pass reads it. Never stops the desk."""
    from trading_research_agents.data.pull import PullError, due, pull
    from trading_research_agents.data.series import SeriesError, load_series_map

    said: list[str] = []
    now = datetime.now(UTC)
    for path in declarations:
        try:
            specs = load_series_map(path)
        except SeriesError:
            continue
        base = path.resolve().parent
        for spec in specs:
            if not due(spec, base, now):
                continue
            try:
                got = pull(spec, base)
            except PullError as exc:
                said.append(
                    f"sources: {exc}; the file on disk is read as it is, and a value past its "
                    "max_age is not counted"
                )
            else:
                said.append(f"sources: {got.series} refreshed, {got.added} new, last {got.last}")
    return said


def _source_lines(declarations: Iterable[Path]) -> tuple[list[str], list[str]]:
    """Per series: where it comes from, the last value, its age, and whether a cycle counts it."""
    from trading_research_agents.data.pull import describe
    from trading_research_agents.data.series import SeriesError, load_series_map

    good: list[str] = []
    stale: list[str] = []
    now = datetime.now(UTC)
    for path in declarations:
        try:
            specs = load_series_map(path)
        except SeriesError:
            continue
        for spec in specs:
            seen = describe(spec, path.resolve().parent, now)
            named = seen["source"] or "no source named"
            head = f"series {seen['series']}: {seen['from']} ({named})"
            if "error" in seen:
                stale.append(f"{head}; {seen['error']}")
            elif seen["counted"]:
                good.append(f"{head}; last {seen['last'][:16]}, {seen['age_h']}h old")
            else:
                stale.append(
                    f"{head}; last {seen['last'][:16]}, {seen['age_h']}h old, past max_age "
                    f"{seen['max_age']}: not counted until it is refreshed"
                )
    return good, stale


def _one_sided(path: Path) -> list[str]:
    """Series on a shelf file whose threshold names only bullish or only bearish lines."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    for entry in raw.get("series") or []:
        threshold = entry.get("threshold") if isinstance(entry, dict) else None
        if not isinstance(threshold, dict) or not threshold:
            continue
        bull = any(str(k).startswith("bullish_") for k in threshold)
        bear = any(str(k).startswith("bearish_") for k in threshold)
        if bull != bear:
            found.append(str(entry.get("name", "?")))
    return found


def _size_notes(cfg: Any) -> list[str]:
    """What one unit of base size means on each instrument this desk can trade."""
    run = cfg.run
    steps = (
        ", ".join(f"{name} {step}" for name, step in run.size_steps.items())
        if run.size_steps
        else str(run.size_step)
    )
    if run.size_quote is not None:
        return [
            f"size: {run.size_quote} of quote currency, converted at each cycle's own mid and "
            f"rounded down to the step ({steps}); a replay still needs [run].size",
            f"size: [controls].max_position {cfg.limits.max_position} is in base units, so it caps "
            "each instrument at that many units whatever they are worth",
        ]
    said = [f"size: {run.size} in base units, step {steps}"]
    if not cfg.universe.idle:
        said.append(
            f"size: {run.size} is a different amount of money on each of "
            f"{', '.join(cfg.universe.instruments)}; [run].size_quote says it once, in quote "
            "currency"
        )
    return said


def _regime_note(cfg: Any) -> str:
    from trading_research_agents.agents.briefs import interval_name

    rule = cfg.regime
    frames = ", ".join(interval_name(s) for s in cfg.run.context_intervals)
    return (
        f"regime: measured on {interval_name(cfg.run.bar_interval_s)}"
        + (f" with {frames} as context" if frames else ", no higher timeframe declared")
        + f"; trending at {rule.trend_above}+, ranging at {rule.range_below} or less; "
        + (
            "a setup that does not fit is refused"
            if rule.setups_must_fit
            else "a setup that does not fit is noted and allowed"
        )
    )


def _load_notes(a: Any, roster: tuple[str, ...]) -> list[str]:
    """How a cycle's calls land on providers, and what happens when one of them says no."""
    asked = list(a.analysts or roster or ("technical_analyst",))
    roles = [*asked, *(["validator"] if a.validators else [])]
    calls = len(asked) + len(a.validators)
    on = sorted({a.for_role(role).provider for role in roles})
    said = [
        f"models: a full cycle makes about {calls} call(s) within a minute, on {', '.join(on)}; "
        f"a provider that says wait up to {a.max_retry_wait_s:.0f}s is waited for once, then the "
        "next link is tried"
    ]
    if len(on) == 1 and calls >= 4:
        said.append(
            f"models: every role is on {on[0]}; if your tier limits tokens per minute, give the "
            'validators their own provider with [agents.roles.validator] provider = "..."'
        )
    if a.validators:
        said.append(
            "models: a declared validator that no model answers "
            + (
                "refuses the trade"
                if a.on_missing_validator == "refuse"
                else "is left out and the trade goes on"
            )
            + " ([agents].on_missing_validator)"
        )
    return said


def _capital_notes(cfg: Any) -> list[str]:
    """What venue mode does with capital, said in the order it happens."""
    mandate = cfg.execution
    hold = {
        "timeout": f"after [exit].timeout_bars ({cfg.exit_rule.timeout_bars} bars)",
        "horizon": "after the analyst's own horizon, capped by [exit].timeout_bars",
        "none": "never by time; only its stop or take profit closes it",
    }[mandate.hold_limit]
    said = [
        f"capital: {mandate.leverage}x {mandate.margin_mode}, applied at the venue before the "
        "first order and read back; an order whose estimated liquidation sits inside the stop "
        f"plus {mandate.liquidation_buffer_bps} bps is refused",
        "capital: sizing uses the smaller of the account's equity and [run].equity; an order "
        f"may use at most {mandate.max_margin_share} of the free margin",
        f"capital: no new entry once equity is down {cfg.limits.daily_loss_limit_bps} bps on the "
        f"day; at most {cfg.limits.max_orders_per_minute} orders a minute",
        f"orders: a taker entry is a limit IOC at most {mandate.max_slippage_bps} bps through the "
        "touch, never a naked market order",
        f"positions: an open position is closed with a reduce-only order {hold}; the kill switch "
        "flattens every position before the run stops",
    ]
    if mandate.hold_limit == "none":
        said.append(
            "positions: hold_limit is none, so a position the market never takes to its stop or "
            "target stays open, and holds back new entries on correlated instruments"
        )
    return said


def _size_against_cap(cfg: Any, cap: Decimal, good: list[str], bad: list[str]) -> None:
    """Two declarations that only meet at the last gate, after a cycle has been paid for.

    Where they stop agreeing is arithmetic, so it is said here instead of at the venue.
    """
    run = cfg.run
    top = max((rung.size_multiple for rung in cfg.conviction.ladder), default=Decimal(1))
    if run.size_quote is None:
        if run.size > 0:
            ceiling = (cap / run.size).quantize(Decimal("0.01"))
            good.append(
                f"execution: {run.size} at a price above {ceiling} is more than {cap} of "
                "notional, and every order would be refused at the venue gate; that is the size "
                "and the cap talking to each other"
            )
        return
    if run.size_quote > cap:
        bad.append(
            f"[run].size_quote {run.size_quote} is above [execution].max_notional {cap}, so every "
            "order would be refused at the venue gate after the cycle had been paid for"
        )
    elif run.size_quote * top > cap:
        good.append(
            f"execution: the top rung of the ladder asks for {run.size_quote * top} of notional "
            f"and the cap is {cap}; orders at that rung are refused at the venue gate"
        )
    else:
        good.append(
            f"execution: at most {run.size_quote * top} of notional per order on any instrument, "
            f"inside the cap of {cap}"
        )


def _desk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What the whole book may carry at once, and whether that can actually be measured."""
    desk = cfg.desk
    if desk.idle:
        return [
            "desk: not declared, so nothing bounds the book as a whole; [desk].max_open_risk_bps "
            "is the sum of what every open position loses at its own stop"
        ]
    said = []
    if desk.max_open_risk_bps is not None:
        said.append(f"at most {desk.max_open_risk_bps} bps of equity at risk across the book")
    if desk.max_open_positions is not None:
        said.append(f"at most {desk.max_open_positions} position(s) open at once")
    if desk.correlation_above is not None:
        said.append(
            f"a candidate moving with an open position at {desk.correlation_above} or above is "
            "refused by the screen"
        )
    if desk.max_open_risk_bps is not None and cfg.limits.max_risk_per_trade_bps is None:
        bad.append(
            "[desk].max_open_risk_bps sums what each position risks, and "
            "[controls].max_risk_per_trade_bps is what puts that figure on a trade; declare it, or "
            "the sum has nothing to add up"
        )
    if desk.correlation_above is not None and cfg.universe.idle:
        bad.append(
            "[desk].correlation_above compares one instrument with another and [universe] names "
            "only one; declare the instruments this desk watches or drop the threshold"
        )
    return ["desk: " + "; ".join(said)]


def _universe_notes(cfg: Any, bad: list[str]) -> list[str]:
    """What this desk watches, and what a pass over it will do."""
    universe = cfg.universe
    if universe.idle:
        return [
            f"universe: not declared, so every cycle is {cfg.run.instrument} from [run]; "
            "[universe].instruments is where a desk names what else it watches"
        ]
    said = [
        f"universe: {', '.join(universe.instruments)}; a pass looks at each for the price of a "
        f"data pull and runs {universe.max_per_cycle} of them"
    ]
    if universe.max_intents_per_day is not None:
        said.append(
            f"universe: at most {universe.max_intents_per_day} intent(s) a day across every "
            "instrument, counted from what is on disk"
        )
    if universe.screener:
        said.append(
            "universe: the screener is asked to narrow what the trigger admitted; it cannot add an "
            "instrument or change the order"
        )
    if cfg.trigger.idle:
        bad.append(
            "[universe] is declared and [trigger] is not, so every pass would ask the whole roster "
            "about every instrument; [trigger].min_move_bps is what makes a pass cheap"
        )
    return said


def _audit_notes(cfg: Any) -> list[str]:
    """When the auditors are asked without anyone typing the command."""
    policy = cfg.audit
    if policy.idle:
        return [
            "auditors: asked only by hand (`tra audit`); a provider that stops answering halfway "
            "through a run will not be looked at until you look"
        ]
    said = []
    if policy.on_alarm:
        said.append(
            f"the technical auditor wakes on an alarm, at most once per kind every "
            f"{policy.alarm_cooldown_s}s"
        )
    if policy.quant_every_cycles is not None:
        said.append(f"the quant auditor runs every {policy.quant_every_cycles} cycles")
    return ["auditors: " + "; ".join(said) + "; neither one can act on what it finds"]


def _spend_notes(cfg: Any) -> list[str]:
    """What the models may spend, what they are asked to spend it on, and who is counting."""
    a = cfg.agents
    if a is None:
        return []
    out: list[str] = []
    ceilings = []
    if a.token_budget is not None:
        ceilings.append(f"{a.token_budget} tokens for the whole process")
    if a.max_calls_per_hour is not None:
        ceilings.append(f"{a.max_calls_per_hour} calls an hour per role")
    if a.max_output_tokens is not None:
        ceilings.append(f"{a.max_output_tokens} output tokens a call")
    if ceilings:
        out.append(
            "spend: capped at " + "; ".join(ceilings) + "; a cycle that reaches a ceiling stops"
        )
    else:
        out.append(
            "spend: nothing is capped; every call is counted in usage.jsonl, each cycle reports "
            "what it cost, and the auditors read those records"
        )
    if a.effort is None:
        out.append(
            "effort: not declared, so each vendor's own default applies; [agents].effort sets it "
            "for every role and [agents.roles.<role>].effort overrides one"
        )
    else:
        said = [f"{a.effort} for every role"]
        said += [
            f"{role} {table['effort']}"
            for role, table in sorted(a.roles.items())
            if table.get("effort")
        ]
        out.append("effort: " + "; ".join(said))
    quoted = sorted(m for m, (i, o) in a.pricing.items() if i > 0 or o > 0)
    if quoted:
        out.append(
            f"prices: declared for {', '.join(quoted)}, so the spend is counted in money as well"
        )
    else:
        out.append(
            "prices: none quoted, so spend is counted in tokens only; put what you were quoted in "
            "[agents.pricing.<model>] for the money column"
        )
    return out


def _risk_notes(cfg: Any, bad: list[str]) -> list[str]:
    """Whether the risk per trade the operator declared can actually be applied to a size."""
    allowed = cfg.limits.max_risk_per_trade_bps
    if allowed is None:
        return [
            "risk per trade: not declared; the conviction ladder and [controls].max_position are "
            "all that bound a size"
        ]
    if cfg.exit_rule.stop_sigma_frac is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and no stop is declared, so the loss "
            "at the stop cannot be computed; declare [exit].stop_sigma_frac"
        )
        return []
    if cfg.run.equity is None:
        bad.append(
            f"[controls].max_risk_per_trade_bps is {allowed} and [run].equity is not set, so there "
            "is nothing to measure the loss against"
        )
        return []
    return [
        f"risk per trade: {allowed} bps of equity at the stop, applied to every size the ladder "
        "produces and cut to fit rather than sent"
    ]


def _trigger_notes(cfg: Any) -> list[str]:
    """What a quiet market costs this desk, which is the whole point of the trigger."""
    trigger = cfg.trigger
    if trigger.idle:
        return [
            "trigger: not declared, so every scheduled cycle pays the whole roster whether or not "
            "anything moved; [trigger].min_move_bps is what makes a quiet hour cheap"
        ]
    said = []
    if trigger.min_move_bps is not None:
        said.append(f"{trigger.min_move_bps} bps of movement since the last look")
    if trigger.max_spread_bps is not None:
        said.append(f"a book no wider than {trigger.max_spread_bps} bps")
    if trigger.cooldown_s is not None:
        said.append(f"{trigger.cooldown_s}s since the last intent")
    if trigger.widen_after is not None:
        said.append(f"more movement after {trigger.widen_after} straight refusals")
    return ["trigger: asks the roster only on " + "; ".join(said)]


def data_get(args: argparse.Namespace) -> int:
    """One day of one instrument, from the venue to a dataset the runs can use."""
    venue_name = args.venue
    max_gap = args.max_gap
    if args.config.is_file():
        raw = tomllib.loads(args.config.read_text(encoding="utf-8"))
        venue_name = venue_name or str(raw.get("run", {}).get("venue_key", "")).partition("/")[0]
        declared = raw.get("data", {}).get("max_gap")
        if max_gap is None and declared:
            max_gap = parse_duration(str(declared))
    if not venue_name:
        raise ValueError("no strategy.toml here to take the venue from; pass --venue")
    if max_gap is None:
        max_gap = timedelta(minutes=5)
    day = month = None
    try:
        day = date.fromisoformat(args.when)
    except ValueError:
        month = parse_month(args.when)
    venue = archive_venue(venue_name)
    args.out.mkdir(parents=True, exist_ok=True)
    fetched = argparse.Namespace(
        venue=venue_name, instrument=args.instrument, day=day, month=month, out=args.out, json=False
    )
    data_fetch(fetched)
    url = venue.archive_url(args.instrument, day=day, month=month)
    archive = args.out / url.rsplit("/", 1)[1]
    stem = f"{args.instrument}-{args.when}"
    audited = argparse.Namespace(
        archive=archive,
        venue=venue_name,
        mapping=None,
        instrument=args.instrument,
        max_gap=max_gap,
        max_rejected=Decimal("0.001"),
        manifest=args.out / f"{stem}.manifest.json",
        parquet=args.out / f"{stem}.parquet",
        json=False,
    )
    code = data_audit(audited)
    if code != CLEARED:
        return code
    if getattr(venue, "funding_rates", None) is not None:
        if day is not None:
            start, end = day - timedelta(days=1), day + timedelta(days=1)
        else:
            year, mon = month or (1970, 1)
            start = date(year, mon, 1) - timedelta(days=1)
            end = start + timedelta(days=33)
        funded = argparse.Namespace(
            venue=venue_name,
            instrument=args.instrument,
            start=start,
            end=end,
            out=args.out / f"{args.instrument}-funding.json",
            json=False,
        )
        data_funding(funded)
    print(f"ready: {audited.parquet}  (max gap {max_gap}; now `tra backtest`)")
    return CLEARED


def data_pull(args: argparse.Namespace) -> int:
    """One snapshot: walk each declared chain once, write what answered and what did not."""
    from trading_research_agents.data.sources import load_data_config
    from trading_research_agents.store import cycle_id, take_snapshot

    _load_env(args)
    cfg = load_data_config(args.data_config)
    instrument = args.instrument
    if instrument is None:
        if not args.config.is_file():
            raise ValueError("name an instrument, or run inside a project with strategy.toml")
        instrument = load_strategy_config(args.config).run.instrument
    kinds = list(args.kind) if args.kind else [k.kind for k in cfg.kinds]
    chains = {kind: cfg.chain(kind) for kind in kinds}
    options = {k.kind: dict(k.options) for k in cfg.kinds}
    cycle = cycle_id()
    snapshot = take_snapshot(instrument, chains, cycle=cycle, kinds=kinds, options=options)
    out = args.out
    if out is None:
        # Beside the cycles, not among them: a pull decided nothing, and everything that walks
        # runs/cycles - the trigger, the desk, the auditors, tra watch - reads only cycles.
        out = Path("runs") / "pulls" / cycle / "snapshot.json"
        out.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write(out)
    payload = snapshot.record()
    lines = [f"snapshot {cycle} {instrument}: {len(snapshot.values)} of {len(kinds)} kinds"]
    for kind, fetched in sorted(snapshot.values.items()):
        row = fetched.record()
        lines.append(f"  {kind:<14} {row['source']:<18} {row['tier']:<5} age {row['age_s']}s")
    for attempt in snapshot.attempts:
        if attempt.outcome != "ok":
            lines.append(f"  tried {attempt.source} for {attempt.kind}: {attempt.detail}")
    for kind, reason in snapshot.refused.items():
        lines.append(f"  refused {kind}: {reason}")
    if snapshot.stale:
        lines.append(f"stale, and used because the config allows it: {', '.join(snapshot.stale)}")
    lines.append(f"snapshot.json in {out.parent}")
    emit(payload, args.json, lines, lambda: _ui().snapshot_view(payload))
    return CLEARED if snapshot.usable else REFUSED


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
    from trading_research_agents.agents.session import ScreenSettings, SessionConfig, run_session
    from trading_research_agents.llm.usage import (
        BudgetLimits,
        MeteredCompleter,
        Pricing,
        UsageLedger,
    )

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("a session needs [strategy].agent and [agents] in strategy.toml")
    datasets: dict[str, Path] = {}
    for item in args.dataset or ():
        name, sep, path = item.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--dataset takes INSTRUMENT=PATH, got {item!r}")
        datasets[name] = Path(path)
    if not datasets:
        datasets = _datasets(args.config)
    if not datasets:
        raise ValueError(f"no datasets in {_data_dir(args.config)}; run `tra data get` first")
    a = cfg.agents
    magnitude = args.magnitude if args.magnitude is not None else a.magnitude_bps
    if magnitude is None:
        raise ConfigError("the claimed edge: --magnitude, or [agents].magnitude_bps")
    _run_dir(args, "session")
    args.out.mkdir(parents=True, exist_ok=True)
    usage = UsageLedger(args.out / "usage.jsonl")
    limits = BudgetLimits(
        token_budget=a.token_budget,
        max_calls_per_hour=a.max_calls_per_hour,
        min_interval_s=a.min_interval_s,
        call_timeout_s=a.call_timeout_s,
    )
    pricing = {m: Pricing(i, o) for m, (i, o) in a.pricing.items()}
    inner = _build_completer(a)
    metered: dict[str, MeteredCompleter] = {}

    def completer_for(role: str) -> MeteredCompleter:
        # One budget for the whole session; the ledger is shared, the cadence is per role.
        if role not in metered:
            metered[role] = MeteredCompleter(
                inner, role=role, provider=a.provider, limits=limits, ledger=usage, pricing=pricing
            )
        return metered[role]

    def session(on_step: Any) -> Any:
        return run_session(
            SessionConfig(
                strategy=cfg,
                envelope=load_envelope(args.envelope),
                datasets=datasets,
                out_dir=args.out,
                completer_for=completer_for,
                usage=usage,
                magnitude_bps=magnitude,
                ladder_path=args.ladder,
                kill_file=cfg.kill_switch,
                macro_series=args.macro,
                onchain_series=args.onchain,
                events=args.events,
                screen=ScreenSettings(**a.screen) if a.screen else ScreenSettings(),
                observation=_observation_beside(args.config, args.observation),
                resume=bool(args.resume),
                on_step=on_step,
            )
        )

    if _watching(args.json):
        with _ui().SessionView(usage) as view:
            result = session(view.on_step)
    else:
        result = session(None)
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
    emit(payload, args.json, lines, lambda: _ui().session_report(result, usage, str(args.out)))
    # A session that never reached its audit verdict (a role refused, a vendor limit) is not
    # cleared, whatever the runs earned.
    return CLEARED if result.stopped_by is None and result.audit is not None else REFUSED


def agent_eval(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.evals import EvalConfig, ModelSpec, evaluate, table

    _load_env(args)
    cfg = load_strategy_config(args.config)
    if cfg.agents is None:
        raise ConfigError("an eval needs [strategy].agent and [agents] in strategy.toml")
    models = tuple(ModelSpec.parse(text) for text in args.model)
    if args.dataset:
        name, sep, path = args.dataset.partition("=")
        if not sep or not name or not path:
            raise ValueError(f"--dataset takes INSTRUMENT=PATH, got {args.dataset!r}")
        instrument, dataset = name, Path(path)
    else:
        found = _datasets(args.config)
        wanted = cfg.run.instrument
        if wanted in found:
            instrument, dataset = wanted, found[wanted]
        elif len(found) == 1:
            ((instrument, dataset),) = found.items()
        else:
            raise ValueError(
                f"no dataset for {wanted!r} in {_data_dir(args.config)}; --dataset INSTRUMENT=PATH"
            )
    _run_dir(args, "eval")
    a = cfg.agents

    def completer_for(spec: ModelSpec) -> Completer:
        p = provider(spec.provider)
        base_url = args.base_url or a.base_url
        if p.build_with is not None:
            return p.build_with(spec.model, base_url, _call_options(a))
        return p.build(spec.model, base_url)

    rows = evaluate(
        EvalConfig(
            strategy=cfg,
            envelope=load_envelope(args.envelope),
            instrument=instrument,
            dataset=dataset,
            models=models,
            completer_for=completer_for,
            out_dir=args.out,
            kill_file=cfg.kill_switch,
        )
    )
    lines = [f"eval {instrument} over {dataset}: {len(rows)} models, same bars, same controls"]
    lines.extend(table(rows))
    for r in rows:
        if r.stopped_by:
            lines.append(f"  {r.provider}:{r.model} stopped by: {r.stopped_by}")
    lines.append(f"eval.json and one run per model in {args.out}")
    payload = {
        "instrument": instrument,
        "dataset": str(dataset),
        "rows": [r.record() for r in rows],
        "out": str(args.out),
    }
    emit(payload, args.json, lines, lambda: _ui().eval_table(rows, instrument, str(dataset)))
    return CLEARED


def agent_trace(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.trace import lines as trace_lines
    from trading_research_agents.agents.trace import trace

    t = trace(args.path)
    emit(t.record(), args.json, trace_lines(t), lambda: _ui().trace_view(t))
    return CLEARED


def signals_export(args: argparse.Namespace) -> int:
    from trading_research_agents.agents.executor.tools import export_signals

    summary = args.run / "run.json"
    if not summary.is_file():
        raise ValueError(f"{args.run} is not a run directory (no run.json)")
    instrument = str(json.loads(summary.read_text("utf-8")).get("instrument", ""))
    out = args.out or (args.run / f"signals.{args.format}")
    n = export_signals(args.run, instrument=instrument, out=out, fmt=args.format)
    emit({"signals": n, "out": str(out)}, args.json, [f"{n} signals -> {out}"])
    return CLEARED


def mcp_serve(args: argparse.Namespace) -> int:
    from trading_research_agents.mcp import serve

    serve(getattr(args, "root", None))
    return CLEARED


def ladder_show(args: argparse.Namespace) -> int:
    ladder = Ladder(args.path)
    lines = [f"tier {ladder.tier.value} {ladder.tier.name}"]
    for h in ladder.history:
        detail = h.get("evidence") or h.get("reason")
        when = str(h["at"])[:19].replace("T", " ")
        lines.append(f"  {when} UTC  {h['kind']} {h['from']} -> {h['to']}: {detail}")
    emit(
        {"tier": ladder.tier.name, "history": list(ladder.history)},
        args.json,
        lines,
        lambda: _ui().ladder_table(f"{ladder.tier.value} {ladder.tier.name}", ladder.history),
    )
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
    emit(
        payload,
        args.json,
        lines,
        lambda: _ui().ledger_table(payload["rows"], payload["counts"]),
    )
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
    ("agent", "eval"): agent_eval,
    ("agent", "trace"): agent_trace,
    ("data", "fetch"): data_fetch,
    ("data", "audit"): data_audit,
    ("data", "funding"): data_funding,
    ("data", "get"): data_get,
    ("data", "pull"): data_pull,
    ("check", "check"): check_project,
    ("mandate", "mandate"): show_mandate,
    ("venues", "venues"): show_venues,
    ("sources", "show"): sources_show,
    ("runs", "verify"): runs_verify,
    ("runs", "prune"): runs_prune,
    ("sources", "pull"): sources_pull,
    ("audit", "audit"): run_audit_command,
    ("keys", "keys"): show_keys,
    ("watch", "watch"): watch_desk,
    ("run", "run"): run_cycle_command,
    ("chain", "floor"): chain_floor,
    ("chain", "observe"): chain_observe,
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
    ("signals", "export"): signals_export,
    ("ladder", "show"): ladder_show,
    ("ladder", "promote"): ladder_promote,
    ("ladder", "demote"): ladder_demote,
}


if __name__ == "__main__":
    sys.exit(main())
