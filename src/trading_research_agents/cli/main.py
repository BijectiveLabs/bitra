"""`tra`: the gates from a shell.

Exit codes are the verdicts a pipeline can act on: 0 when the gate was cleared or the work is
done, 1 when a gate said no (an audit failed, an idea stopped at the cost floor, a file did not
validate), 2 for a usage error. Errors go to stderr; results go to stdout, as text or JSON.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from trading_research_agents import __version__
from trading_research_agents.agents.briefs import BriefError
from trading_research_agents.agents.context import (
    ContextError,
)
from trading_research_agents.agents.cycle import CycleError
from trading_research_agents.agents.session import SessionError
from trading_research_agents.chain import (
    EraLeakageError,
    ValidationError,
)
from trading_research_agents.chain.preregistration import PreregistrationError
from trading_research_agents.cli import console
from trading_research_agents.cli.arena import add_arena_command
from trading_research_agents.cli.check import NOT_A_DESK, _check, check_project
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _watching,
    parse_month,
    plain_asked,
    set_plain,
)
from trading_research_agents.cli.console import HISTORY_FILE
from trading_research_agents.cli.data_commands import (
    data_audit,
    data_bars_command,
    data_fetch,
    data_funding,
    data_get,
    data_pull,
    parse_duration,
    runs_prune,
    runs_verify,
    show_universe,
    show_venues,
    sources_add,
    sources_pull,
    sources_show,
)
from trading_research_agents.cli.init_command import init_project
from trading_research_agents.cli.market import add_bars_command
from trading_research_agents.cli.record import add_record_command
from trading_research_agents.cli.research_commands import (
    agent_eval,
    agent_propose,
    agent_refusals,
    agent_session,
    agent_trace,
    chain_check,
    chain_floor,
    chain_observe,
    chain_requirements,
    chain_validate,
    ladder_demote,
    ladder_promote,
    ladder_show,
    ledger_show,
    mcp_serve,
    signals_export,
    trade_live,
    trade_replay,
    trade_shadow,
)
from trading_research_agents.cli.review_commands import (
    arena_command,
    hindsight_command,
    record_command,
    record_status_command,
    run_audit_command,
    sizing_command,
    text_judge_command,
)
from trading_research_agents.cli.run_command import run_cycle_command
from trading_research_agents.cli.text import (
    add_text_command,
)
from trading_research_agents.cli.venue_commands import (
    kill_command,
    pause_command,
    show_keys,
    show_mandate,
    show_models,
    watch_desk,
)
from trading_research_agents.data import (
    ArchiveError,
    ArchiveFormatError,
    MappingError,
    UnknownVenueError,
    archive_venues,
)
from trading_research_agents.data.fetch import FetchError
from trading_research_agents.data.parquet import ParquetError
from trading_research_agents.data.sources import (
    ChainExhaustedError,
    DataConfigError,
    SourceError,
)
from trading_research_agents.data.sources.registry import UnknownSourceError
from trading_research_agents.envelope import EnvelopeError
from trading_research_agents.execution.pretrade import PreTradeError
from trading_research_agents.ledger import LedgerError
from trading_research_agents.llm.completer import ProviderError
from trading_research_agents.llm.providers import (
    UnknownProviderError,
    providers,
)
from trading_research_agents.llm.providers.anthropic import DEFAULT_MODEL
from trading_research_agents.strategy import ExitError
from trading_research_agents.trading import (
    ConfigError,
    ControlError,
    LadderError,
    Tier,
    VenueError,
)
from trading_research_agents.trading.canary import CanaryError
from trading_research_agents.trading.feed import FeedError
from trading_research_agents.trading.feeds import UnknownFeedError
from trading_research_agents.trading.venues import (
    UnknownLiveVenueError,
)


def parse_decimal(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None


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
    init_cmd.add_argument(
        "--venue",
        metavar="VENUE",
        help="the exchange (its ccxt id) the desk reads and trades on",
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

    models_cmd = top.add_parser(
        "models", help="the models your key can use on a provider; no prompt is sent"
    )
    models_cmd.set_defaults(command="models")
    models_cmd.add_argument("provider", nargs="?", help="default: [agents].provider")
    models_cmd.add_argument("--filter", default="", help="only names containing this text")
    models_cmd.add_argument("--base-url", default=None)
    models_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    models_cmd.add_argument("--env-file", type=Path, default=None)
    models_cmd.add_argument("--json", action="store_true")

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
    keys_cmd.add_argument(
        "--prepare",
        action="store_true",
        help="probe, and on a demo or testnet API set the account's position mode to one-way when "
        "it is hedged and nothing is open; never on mainnet",
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

    for name, said in (
        ("pause", "hold every new entry; open positions keep their exits (--off lifts it)"),
        ("kill", "flatten and stop a running desk at its next pass (--off disarms)"),
    ):
        switch_cmd = top.add_parser(name, help=said)
        switch_cmd.set_defaults(command=name)
        switch_cmd.add_argument("--off", action="store_true", help="lift it")
        switch_cmd.add_argument("--reason", default="", help="written into the file, and shown")
        switch_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
        switch_cmd.add_argument("--json", action="store_true")

    hindsight_cmd = top.add_parser(
        "hindsight",
        help="what the trades each gate refused would have done, beside the ones it let through",
    )
    hindsight_cmd.set_defaults(command="hindsight")
    hindsight_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    hindsight_cmd.add_argument("--data-config", type=Path, default=Path("data.toml"))
    hindsight_cmd.add_argument("--runs", type=Path, default=Path("runs") / "cycles")
    hindsight_cmd.add_argument(
        "--hold",
        type=parse_duration_s,
        default=None,
        help="how long each trade is held (default: [exit].timeout_bars on the decision bar)",
    )
    hindsight_cmd.add_argument(
        "--bar", type=parse_duration_s, default=300.0, help="the replay's candle (default: 5m)"
    )
    hindsight_cmd.add_argument("--days", type=int, default=None, help="only the last N days")
    hindsight_cmd.add_argument("--json", action="store_true")
    add_arena_command(top)
    add_record_command(top)
    add_text_command(top)

    sizing_cmd = top.add_parser(
        "sizing",
        help="what the desk's own round trips support: Kelly, the drawdown ahead, the excursions",
    )
    sizing_cmd.set_defaults(command="sizing")
    sizing_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    sizing_cmd.add_argument("--runs", type=Path, default=Path("runs") / "cycles")
    sizing_cmd.add_argument(
        "--fraction",
        type=_kelly_fraction,
        default=None,
        help="the Kelly fraction to show when [sizing] is not declared (default: 0.25)",
    )
    sizing_cmd.add_argument("--json", action="store_true")

    universe_cmd = top.add_parser(
        "universe", help="what discovery finds on the venue now, and what each filter took out"
    )
    universe_cmd.set_defaults(command="universe")
    universe_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    universe_cmd.add_argument("--json", action="store_true")

    runs = top.add_parser(
        "runs", help="check a desk's records against their hashes and inputs, or prune old ones"
    ).add_subparsers(dest="command", required=True)
    verify_cmd = runs.add_parser(
        "verify", help="every cycle: files against their hashes, briefs against their inputs"
    )
    verify_cmd.add_argument("--days", type=int, default=None, help="only the last N days")
    verify_cmd.add_argument(
        "--market",
        type=Path,
        default=None,
        help="check the market archive under this data directory instead",
    )
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
    add = sources.add_parser(
        "add",
        help="put a fetched series on the shelf: pulled now, its lines drawn from its own history",
        epilog=(
            "example: tra sources add macro us10y --from fred --id DGS10 --high bearish; "
            "tra sources add onchain eth_net_flow --from coinmetrics --id FlowInExNtv "
            "--minus FlowOutExNtv --fetch-asset eth --asset ETH --high bearish --band 10; "
            "tra sources add derivatives btc_liquidations --from coinglass "
            "--id futures/liquidation/aggregated-history --param symbol=BTC --param interval=1h "
            "--value aggregated_short_liquidation_usd --minus aggregated_long_liquidation_usd "
            "--asset BTC --high bullish"
        ),
    )
    add.add_argument("kind", choices=("macro", "sentiment", "onchain", "derivatives"))
    add.add_argument("name", help="the series' name in briefs and records")
    add.add_argument("--from", dest="origin", required=True, help="a preset, or http")
    add.add_argument("--id", default=None, help="the source's own name for the series")
    add.add_argument("--minus", default=None, help="a second series subtracted from the first")
    add.add_argument("--fetch-asset", default=None, help="the asset the source is asked about")
    add.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="a query field the source asks for, such as symbol=BTCUSDT; repeat for more",
    )
    for flag in (
        "--url",
        "--rows",
        "--ts",
        "--ts-unit",
        "--value",
        "--key-env",
        "--key-param",
        "--key-header",
        "--key-prefix",
    ):
        add.add_argument(flag, default=None, help="http sources: see docs/sources")
    add.add_argument(
        "--high",
        required=True,
        choices=("bullish", "bearish"),
        help="whether a high value is good or bad for the trade; the one thing asked of you",
    )
    add.add_argument(
        "--band", type=int, default=25, help="percentile for the lines; 25 is quartiles"
    )
    add.add_argument("--days", type=int, default=365, help="the history the lines are drawn from")
    add.add_argument("--asset", default="", help="the asset the series is about, such as BTC")
    add.add_argument("--unit", default="")
    add.add_argument("--config", type=Path, default=Path("strategy.toml"))
    add.add_argument("--env-file", type=Path, default=None)
    add.add_argument("--json", action="store_true")

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
    venues_cmd.add_argument(
        "--environment",
        choices=("demo", "testnet", "mainnet"),
        help="with --setup: where to read your fees; default is the venue's demo or testnet API",
    )
    venues_cmd.add_argument(
        "--instrument", default="BTCUSDT", help="with --setup: the market to read fees for"
    )
    venues_cmd.add_argument(
        "--env-file", type=Path, default=None, help="where the keys are read from"
    )
    venues_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    venues_cmd.add_argument("--json", action="store_true")

    mandate_cmd = top.add_parser(
        "mandate",
        help="print the execution mandate; venue mode needs it in the project, word for word",
    )
    mandate_cmd.set_defaults(command="mandate")
    mandate_cmd.add_argument(
        "--sign",
        metavar="NAME",
        default=None,
        help="write MANDATE.md into the project and sign [execution] with this name and today",
    )
    mandate_cmd.add_argument(
        "--on", type=date.fromisoformat, default=None, help="the signing date (default: today)"
    )
    mandate_cmd.add_argument(
        "--venue-mode",
        action="store_true",
        help='with --sign: also set [execution].mode = "venue", so orders go out once `tra check` '
        "passes",
    )
    mandate_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
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
    promote = ladder.add_parser(
        "promote",
        help="one step up, with evidence and a signature",
        epilog=(
            'example: tra ladder promote --signed-by "your name" '
            '--evidence "sandbox cycle on live data: gates and records verified"'
        ),
    )
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
    set_plain(bool(args.plain))
    if args.once is not None:
        return _console_for(parser, args, interactive=False).one(args.once, say=False) or CLEARED
    if args.group is None and console.interactive() and not plain_asked():
        # A person at a terminal gets somewhere to stand, not one screen and their shell back.
        return _console_for(parser, args).loop()
    handler = home if args.group is None else HANDLERS[(args.group, args.command)]
    return _dispatch(handler, args)


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
        home=lambda: home(args),
        commands=typed_commands(parser),
        set_plain=set_plain,
        history=console.history_path(bool(args.history)) if interactive else None,
        read=None if interactive else (lambda: ""),
    )


def typed_commands(parser: argparse.ArgumentParser) -> list[str]:
    """Every command as a person types it: `run`, `data get`, never the handler's own key."""
    out: list[str] = []
    for name, sub in sorted(_choices(parser).items()):
        children = _choices(sub)
        if children:
            # A group that runs on its own as well, as `tra record` beside `tra record status`.
            if sub.get_default("command") not in (None, *children):
                out.append(name)
            out.extend(f"{name} {child}" for child in sorted(children))
        else:
            out.append(name)
    return out


def _choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


DESK_FILES = frozenset({"strategy.toml", "envelope.toml", "data.toml"})


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
    except FileNotFoundError as exc:
        name = Path(str(exc.filename or "")).name
        print(f"tra: {name or exc}: not found in {Path.cwd()}", file=sys.stderr)
        if name in DESK_FILES:
            print(f"tra: this folder is not a desk; {NOT_A_DESK}", file=sys.stderr)
        return REFUSED
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


def home(args: argparse.Namespace) -> int:
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
    tui.home(__version__, good, bad, next_command)
    return CLEARED


def parse_duration_s(text: str) -> float:
    """`30s`, `5m`, `2h` to seconds, for a flag. The same spellings data.toml already takes."""
    from trading_research_agents.data.sources import parse_duration

    seconds = parse_duration(text)
    if seconds < 5:
        raise argparse.ArgumentTypeError("an interval below five seconds is not a trading cadence")
    return seconds


def _kelly_fraction(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except ArithmeticError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or not Decimal(0) < value <= 1:
        raise argparse.ArgumentTypeError("the Kelly fraction is above 0 and at most 1")
    return value


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
    ("data", "bars"): data_bars_command,
    ("data", "pull"): data_pull,
    ("check", "check"): check_project,
    ("mandate", "mandate"): show_mandate,
    ("venues", "venues"): show_venues,
    ("sources", "show"): sources_show,
    ("universe", "universe"): show_universe,
    ("runs", "verify"): runs_verify,
    ("runs", "prune"): runs_prune,
    ("sources", "pull"): sources_pull,
    ("sources", "add"): sources_add,
    ("models", "models"): show_models,
    ("audit", "audit"): run_audit_command,
    ("hindsight", "hindsight"): hindsight_command,
    ("arena", "arena"): arena_command,
    ("record", "run"): record_command,
    ("record", "status"): record_status_command,
    ("text", "judge"): text_judge_command,
    ("sizing", "sizing"): sizing_command,
    ("pause", "pause"): pause_command,
    ("kill", "kill"): kill_command,
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
