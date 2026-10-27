"""The `tra` command line: every command, its arguments and its help, family by family."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from trading_research_agents import __version__
from trading_research_agents.cli.arena import add_arena_command
from trading_research_agents.cli.arguments import parse_decimal
from trading_research_agents.cli.common import parse_month
from trading_research_agents.cli.console import HISTORY_FILE
from trading_research_agents.cli.data_commands import parse_duration
from trading_research_agents.cli.market import add_bars_command
from trading_research_agents.cli.parser_desk import (
    _add_audit,
    _add_check,
    _add_hindsight,
    _add_init,
    _add_keys,
    _add_ladder,
    _add_ledger,
    _add_mandate,
    _add_models,
    _add_run,
    _add_runs,
    _add_signals,
    _add_sizing,
    _add_sources,
    _add_switches,
    _add_universe,
    _add_venues,
    _add_watch,
)
from trading_research_agents.cli.record import add_record_command
from trading_research_agents.cli.text import (
    add_text_command,
)
from trading_research_agents.data import (
    archive_venues,
)
from trading_research_agents.llm.providers import (
    providers,
)
from trading_research_agents.llm.providers.anthropic import DEFAULT_MODEL


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
    venues = archive_venues()
    _add_data(top, venues)
    _add_chain(top)
    _add_agent(top)
    _add_trade(top)
    _add_mcp(top)
    _add_init(top)
    _add_signals(top)
    _add_run(top)
    _add_watch(top)
    _add_models(top)
    _add_keys(top)
    _add_check(top)
    _add_audit(top)
    _add_switches(top)
    _add_hindsight(top)
    add_arena_command(top)
    add_record_command(top)
    add_text_command(top)
    _add_sizing(top)
    _add_universe(top)
    _add_runs(top)
    _add_sources(top)
    _add_venues(top)
    _add_mandate(top)
    _add_ladder(top)
    _add_ledger(top)
    return parser


def _add_data(top: Any, venues: tuple[str, ...]) -> None:
    data = top.add_parser("data", help="fetch and audit trade data").add_subparsers(
        dest="command", required=True
    )
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
    add_bars_command(data, venues)
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
    audit_cmd.add_argument(
        "archive", type=Path, help=".csv, .csv.gz, .zip of one CSV, or .parquet with --mapping"
    )
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


def _add_chain(top: Any) -> None:
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


def _add_agent(top: Any) -> None:
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
        "--macro", type=Path, help="a series.toml of macro series for the macro reading"
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


def _add_trade(top: Any) -> None:
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


def _add_mcp(top: Any) -> None:
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
