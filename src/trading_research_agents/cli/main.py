"""`tra`: the gates from a shell.

Exit codes are the verdicts a pipeline can act on: 0 when the gate was cleared or the work is
done, 1 when a gate said no (an audit failed, an idea stopped at the cost floor, a file did not
validate), 2 for a usage error. Errors go to stderr; results go to stdout, as text or JSON.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
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
from trading_research_agents.cli.check import NOT_A_DESK, _check, check_project
from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _watching,
    plain_asked,
    set_plain,
)
from trading_research_agents.cli.data_commands import (
    data_audit,
    data_bars_command,
    data_fetch,
    data_funding,
    data_get,
    data_pull,
    runs_prune,
    runs_verify,
    show_universe,
    show_venues,
    sources_add,
    sources_pull,
    sources_show,
)
from trading_research_agents.cli.init_command import init_project
from trading_research_agents.cli.parser import build_parser
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
)
from trading_research_agents.strategy import ExitError
from trading_research_agents.trading import (
    ConfigError,
    ControlError,
    LadderError,
    VenueError,
)
from trading_research_agents.trading.canary import CanaryError
from trading_research_agents.trading.feed import FeedError
from trading_research_agents.trading.feeds import UnknownFeedError
from trading_research_agents.trading.venues import (
    UnknownLiveVenueError,
)


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
