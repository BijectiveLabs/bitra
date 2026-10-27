"""The desk's own commands: cycles, watching, keys and models, checks, audits and the venue."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from trading_research_agents.cli.arguments import _kelly_fraction, parse_decimal, parse_duration_s
from trading_research_agents.cli.data_commands import parse_duration
from trading_research_agents.trading import (
    Tier,
)


def _add_init(top: Any) -> None:
    init_cmd = top.add_parser("init", help="a new project directory from the templates")
    init_cmd.set_defaults(command="init")
    init_cmd.add_argument("directory", type=Path)
    init_cmd.add_argument(
        "--agent", metavar="PROVIDER", help="make the structure lead the core, on this provider"
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


def _add_signals(top: Any) -> None:
    signals = top.add_parser(
        "signals", help="a run's admitted intents, for execution elsewhere"
    ).add_subparsers(dest="command", required=True)
    export_cmd = signals.add_parser("export", help="signals.jsonl or .csv from a run directory")
    export_cmd.add_argument("run", type=Path, help="a run directory with intents.jsonl")
    export_cmd.add_argument("--out", type=Path, default=None, help="default: <run>/signals.jsonl")
    export_cmd.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    export_cmd.add_argument("--json", action="store_true")


def _add_run(top: Any) -> None:
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


def _add_watch(top: Any) -> None:
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


def _add_models(top: Any) -> None:
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


def _add_keys(top: Any) -> None:
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


def _add_check(top: Any) -> None:
    check_cmd = top.add_parser("check", help="what is ready and what is missing, in plain words")
    check_cmd.set_defaults(command="check")
    check_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    check_cmd.add_argument("--envelope", type=Path, default=Path("envelope.toml"))
    check_cmd.add_argument("--ladder", type=Path, default=Path("ladder.json"))
    check_cmd.add_argument("--env-file", type=Path, default=None)
    check_cmd.add_argument("--json", action="store_true")


def _add_audit(top: Any) -> None:
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


def _add_switches(top: Any) -> None:
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


def _add_hindsight(top: Any) -> None:
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


def _add_sizing(top: Any) -> None:
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


def _add_universe(top: Any) -> None:
    universe_cmd = top.add_parser(
        "universe", help="what discovery finds on the venue now, and what each filter took out"
    )
    universe_cmd.set_defaults(command="universe")
    universe_cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    universe_cmd.add_argument("--json", action="store_true")


def _add_runs(top: Any) -> None:
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


def _add_sources(top: Any) -> None:
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


def _add_venues(top: Any) -> None:
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


def _add_mandate(top: Any) -> None:
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


def _add_ladder(top: Any) -> None:
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


def _add_ledger(top: Any) -> None:
    ledger_p = top.add_parser("ledger", help="the kill ledger").add_subparsers(
        dest="command", required=True
    )
    show_cmd = ledger_p.add_parser("show", help="the kill count and every row that stands")
    show_cmd.add_argument("--path", type=Path, default=Path("ledger.jsonl"))
    show_cmd.add_argument("--json", action="store_true")
