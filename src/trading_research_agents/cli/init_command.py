"""`tra init`: a new desk from the templates, with the venue's own settings filled in."""

from __future__ import annotations

import argparse
import re
from importlib.resources import files
from pathlib import Path
from typing import Any

from trading_research_agents.cli.common import CLEARED, _environment_words, emit
from trading_research_agents.llm.providers import (
    provider,
)

TEMPLATE_FILES = {
    "strategy.toml": "strategy.toml",
    "envelope.toml": "envelope.toml",
    "mapping.toml": "mapping.toml",
    "data.toml": "data.toml",
    "observation.toml": "observation.toml",
    # One declaration per class of data, so a vendor is a line in a file rather than a file of
    # its own. A reading is read only when its class is declared.
    "sources/macro.toml": "sources/macro.toml",
    "sources/onchain.toml": "sources/onchain.toml",
    "sources/derivatives.toml": "sources/derivatives.toml",
    "sources/sentiment.toml": "sources/sentiment.toml",
    "sources/news.toml": "sources/news.toml",
    "strategies/trailing_zscore.py": "core.py.tmpl",
    "strategies/momentum_long.py": "momentum_long.py.tmpl",
    ".env.example": "env.example",
    ".gitignore": "gitignore",
    "README.md": "README.md",
}


def init_project(args: argparse.Namespace) -> int:
    target: Path = args.directory
    templates = files("trading_research_agents").joinpath("templates")
    existing = [name for name in TEMPLATE_FILES if (target / name).exists()]
    if existing:
        raise ValueError(f"{target}: already has {existing}; init never overwrites")
    autonomous = args.profile == "autonomous"
    able: Any = None
    keys: list[str] = []
    starter = ""
    if args.venue:
        found = re.search(
            r'venue_key = "([^/"]+)/',
            templates.joinpath("strategy.toml").read_text(encoding="utf-8"),
        )
        starter = found.group(1) if found else ""
        from trading_research_agents.data.sources.capability import capability_of
        from trading_research_agents.trading.hand_state import credentials_for

        able = capability_of(args.venue)
        keys = list(credentials_for(able.venue).values())
    if args.agent and not args.model:
        args.model = provider(args.agent).default_model or ""
        if not args.model:
            raise ValueError(
                f"--agent {args.agent} needs --model; `tra models {args.agent}` lists the ones "
                "your key can use"
            )
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
        if name == "strategy.toml" and args.profile == "rule":
            text = _rule_config(text)
        if name == "README.md" and autonomous:
            # A desk made for the agents starts with what the agents need; the research tools the
            # same file describes come after.
            title, rest = text.split("\n", 1)
            text = title + FIRST_HOUR.rstrip() + "\n\n## The research tools\n" + rest
        if args.venue:
            text = _for_venue(name, text, able, keys, starter)
        out.write_text(text, encoding="utf-8")
    lines = [f"{target}: {len(TEMPLATE_FILES)} files", *(f"  {n}" for n in TEMPLATE_FILES)]
    if args.venue:
        lines.append(_venue_next(able, keys))
    if args.agent:
        key = provider(args.agent).key_env
        if key:
            lines.append(f"put {key} in .env (copy .env.example)")
    if args.profile == "rule":
        lines.append(
            "the desk's one decision is strategies/momentum_long.py, an example rule on "
            "daily bars; replay it with `tra arena --tape klines --rows "
            "strategies/momentum_long.py:MomentumLong`, then write its mean net per trade "
            "into [strategy].edge_bps"
        )
    elif autonomous:
        lines.append(
            "the cycle is wired as the core: one decision, the exits, the cost rule, the risk "
            "gate; the experiments are listed, off, in strategy.toml"
        )
        lines.append(
            "next: put your key in .env, then `tra check`"
            if args.agent
            else "next: put your key in .env, name the provider in [agents], then `tra check`"
        )
    else:
        lines.append("next: cd there, `tra data get BTCUSDT 2025-01-01`, then `tra backtest`")
    emit({"directory": str(target), "files": list(TEMPLATE_FILES)}, args.json, lines)
    return CLEARED


RULE_HEAD = """[strategy]
file = "strategies/momentum_long.py"     # an example, not a recommendation: replay it first
class = "MomentumLong"
# edge_bps = 0                           # the rule's mean net per trade, bps, from your own
#                                        # `tra arena` run of the rule's file; required,
#                                        # because nothing here guesses it
lookback_bars = 200                      # closed candles the rule is handed each decision

[exit]
# The rule names its own stop and take on each trade. This is the widest stop the desk accepts;
# a rule's stop past it refuses the trade rather than being moved.
mode = "analyst"
max_stop_frac = 0.12
timeout_bars = 6                         # six daily bars, the hold the arena measured

"""


def _rule_config(text: str) -> str:
    """strategy.toml with the example daily rule as the desk's one decision, and no model."""
    start, end = text.index("[strategy]"), text.index("[run]")
    text = text[:start] + RULE_HEAD + text[end:]
    return text.replace(
        "bar_interval_s = 60\n",
        "bar_interval_s = 86400                # one decision a day, at the daily close\n",
        1,
    )


def _for_venue(name: str, text: str, able: Any, keys: list[str], starter: str) -> str:
    """One template file, written for the venue the desk reads from and trades on.

    `starter` is the venue the templates were written for. Nothing here guesses a number that
    belongs to the venue or the account: sizes use the venue's own step, the fees are the account's
    and are left blank until read or written, and the environment is the venue's own practice
    account where it has one. Orders stay in sandbox mode until the operator turns venue mode on,
    which has its own door.
    """
    venue = able.venue
    if name == "data.toml":
        text = re.sub(r'"ccxt:[a-z0-9_]+"(?:, "ccxt:[a-z0-9_]+")*', f'"ccxt:{venue}"', text)
        start = text.find("# Measured on ")
        if start != -1:
            text = text[:start].rstrip("\n") + "\n"
        return text
    if able.level == "data":
        return text
    environment = "demo" if able.demo else "testnet" if able.testnet else "mainnet"
    if name == "strategy.toml":
        text = text.replace(
            f'venue_key = "{starter}/taker-taker"', f'venue_key = "{venue}/taker-taker"'
        )
        text = re.sub(
            r"size_step = [0-9.]+ +# the venue's size increment.*",
            "# size_step is left out: sizes use the venue's own step. Declare it to override.",
            text,
        )
        text = text.replace(
            f"{starter.upper()}_API_KEY / {starter.upper()}_API_SECRET", " / ".join(keys)
        )
        text = text.replace(f'venue = "{starter}"', f'venue = "{venue}"')
        stanza = (
            f'venue = "{venue}"\nenvironment = "{environment}"      # {_environment_words(able)}\n'
            "leverage = 3                     # applied at the venue and read back before the "
            "first order\n"
            'margin_mode = "isolated"         # one position never draws on the rest of the '
            "account\n"
            "max_notional = 200               # one order, in quote currency\n" + _exits_line(able)
        )
        text = text.replace("# max_notional = 200\n", "", 1)
        if "\n[targets]\n" in text:
            # The autonomous profile, the only one with a live [targets]. A desk the agents run
            # across several instruments sizes in money, not in coins: 0.01 is a fair BTC order
            # and a meaningless ETH one.
            text = re.sub(
                r"(?m)^size = [0-9.]+ .*$",
                "size_quote = 100                   # quote currency per order",
                text,
                count=1,
            )
        if 'mode = "sandbox"\n' in text:
            text = text.replace('mode = "sandbox"\n', 'mode = "sandbox"\n' + stanza, 1)
        else:
            text = text.replace(
                "\n[data]\n", f'\n[execution]\nmode = "sandbox"\n{stanza}\n[data]\n', 1
            )
        return text
    if name == "envelope.toml":
        return chr(10).join(
            [
                "# What a round trip costs on your venue, per side in basis points (0.05% is 5).",
                "# Left blank, `tra check` fills them: from your account when it reports them,",
                "# otherwise the venue's standard tier, labelled and replaced once it does.",
                "# A fee you write yourself is kept, and checked against your account.",
                "",
                "[envelope]",
                "order_latency_ms = 5.5",
                "",
                "[[channel]]",
                f'venue = "{venue}"',
                'name = "taker-taker"',
                "entry_fee_bps =                  # your taker fee",
                "exit_fee_bps =                   # your taker fee",
                "",
                "[[channel]]",
                f'venue = "{venue}"',
                'name = "maker-taker"',
                "entry_fee_bps =                  # your maker fee",
                "exit_fee_bps =                   # your taker fee",
                "",
            ]
        )
    if name == ".env.example":
        kept = [
            line
            for line in text.splitlines()
            if not line.startswith((f"{starter.upper()}_API_", "# A trade key:"))
        ]
        return "\n".join([*kept, "", f"# {venue}", *(f"{k}=" for k in keys)]) + "\n"
    return text


def _exits_line(able: Any) -> str:
    """`[execution].exits` for what this venue can do through ccxt; empty where attached works."""
    from trading_research_agents.trading.venues.exits import exit_reads

    if able.level != "exits-none":
        return ""
    if able.can_place_exits and exit_reads(able.venue):
        return 'exits = "separate"               # placed right after the fill on this venue\n'
    return 'exits = "none"                   # no stop at this venue; you carry them\n'


def _venue_next(able: Any, keys: list[str]) -> str:
    if able.level == "data":
        return (
            f"{able.venue} feeds this desk's data; it cannot take orders through ccxt "
            f"({able.verdict()})"
        )
    from trading_research_agents.trading.venues.notes import notes_for

    steps = "\n".join(f"  at {able.venue}: {note}" for note in notes_for(able.venue))
    return (
        f"{able.venue}: put {', '.join(keys)} in .env, then `tra keys --probe --prepare` reads the "
        "account and readies it; orders stay in sandbox until "
        f'`tra mandate --sign "Your Name" --venue-mode`\n{steps}'
    )


AGENT_HEAD = (
    "[strategy]\n"
    'agent = "structure_lead"          # the model decides on each closed bar\n'
    "# file = ... / class = ...             # or a scripted strategy instead\n"
    "#                                       # (docs.bijectivelabs.dev/opensource/trading)\n\n"
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


FIXED_EXIT = (
    "stop_frac = 0.01             # 1 percent of price, on the adverse side\n"
    "take_profit_frac = 0.02\n"
)


VOLATILITY_EXIT = (
    'mode = "analyst"             # the analyst names its stop and target, held in bounds\n'
    "min_stop_frac = 0.002        # a stop never tighter than 0.2 percent of price\n"
    "max_stop_frac = 0.03         # and never wider than 3 percent\n"
    "min_stop_atr = 1.0           # nor inside one bar's ordinary range\n"
    "# min_reward_to_risk = 1.5   # an experiment: refuse a target closer than 1.5x the stop\n"
    "max_reward_to_risk = 4.0     # a target past 4x the stop is brought in\n"
    "stop_atr = 1.5               # with no level named: the stop in average true ranges\n"
    "reward_to_risk = 2.0         # and the take profit at twice it\n"
    '# mode = "volatility" measures every stop; mode = "fixed" puts every trade at one distance\n'
)


AGENT_TIMEFRAME_FROM = (
    "bar_interval_s = 60\n"
    '# context_intervals = ["15m", "1h"] # higher timeframes the structure lead '
    "reads as context\n"
)


AGENT_TIMEFRAME = (
    "bar_interval_s = 900                # the decision timeframe: 15m; run with --every 15m\n"
    'context_intervals = ["1h", "4h"]    # higher timeframes the analyst reads as context\n'
)


AGENT_TIMEOUT = (
    "timeout_bars = 16            # four hours of 15m bars; stops are measured over it\n"
)


# A thesis the analyst can trade in any market: it names no single setup, so the regime decides.
AGENT_MECHANISM = (
    "trade the setup the measured regime supports: with a trend, inside a range, or flat"
)


def _agent_config(text: str, provider_name: str, model: str) -> str:
    """The template with the structure lead as the core and the [agents] table live."""
    text = text[: text.index("[strategy]")] + AGENT_HEAD + text[text.index("[exit]") :]
    # An agent desk trades many instruments at many speeds; one fraction of price fits none of them.
    text = text.replace(FIXED_EXIT, VOLATILITY_EXIT, 1)
    # A round trip costs the same on any timeframe, so the move it is weighed against should be
    # worth it: fifteen-minute bars, with the hour and four hours as context. Theirs to change.
    text = text.replace(AGENT_TIMEFRAME_FROM, AGENT_TIMEFRAME, 1)
    # The hold limit is counted in bars, so the same four hours is sixteen of them at 15m.
    text = text.replace("timeout_bars = 240\n", AGENT_TIMEOUT, 1)
    text = _without_commented(text, "# [agents]")
    agents = (
        "[agents]\n"
        f'provider = "{provider_name}"\n'
        f'model = "{model}"\n'
        f'mechanism = "{AGENT_MECHANISM}"\n'
        "lookback_bars = 60\n"
        "# min_confidence = 0.6         # an experiment: stated confidence is uncalibrated\n"
        "magnitude_bps = 20                 # the claimed edge when the analyst names no target\n"
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
[targets]
# The paths are recorded on every cycle as a risk reading, not a gate: drawn from the instrument's
# own candles they are a calibrated random walk, so their value says nothing about the edge
# (Labs evidence, 2026-11-06). Declare min_path_ev_bps only to run that floor as an experiment.
max_round_trips_per_day = 6

[trigger]
# What has to have happened before the roster is paid to have an opinion. All of it is computed from
# the snapshot and the cycles on disk, so a cycle that holds here costs one data pull and no tokens.
min_move_bps = 15
max_spread_bps = 8
cooldown_s = 900

[regime]
# The regime is measured and shown to the lead either way. Whether a setup that does not fit it is
# refused is an experiment like those below: the lead already reads the regime it would be checked
# against.
setups_must_fit = false

# Experiments. Each layer below is off, because none has been measured against the core: data, one
# decision, the exits, the cost rule above, the risk gate and execution. Turn on one at a time and
# keep it only if `tra arena` shows the core with it beating the core without it, out of sample.
# `tra check` lists the ones a desk has on.
#
# [conviction]                     # size by how strongly the voices agree
# weights = { structure_lead = 2.0 }
# min_score = 0.4
# [[conviction.ladder]]
# at_least = 0.75
# size_multiple = 1.0
#
# [confluence]                     # readings that must agree with the direction
# required = ["macro"]
# long_states = ["bullish"]
# short_states = ["bearish"]
#
# [audit]                          # two model auditors that report and never act
# on_alarm = true
# quant_every_cycles = 20
#
# In [regime]: setups_must_fit = true, a setup the measured regime does not support is refused.
# In [trigger]: widen_after = 6, a larger move asked for after a run of refusals; min_move_atr =
#   0.25, the move as a fraction of the instrument's own ATR instead of a fixed bps figure.
# In [targets]: min_edge_multiple = 2.0 and min_net_bps, on the paths' expected value;
#   max_break_even_win_rate = 0.5, on the levels alone.
# In [exit]: min_reward_to_risk = 1.5, on the levels alone.
# In [agents]: min_confidence, on a confidence the model states and nobody calibrated.
# In [agents]: reviewers = ["pro", "contra", "neutral"], team = ["blind_confirmer"], readings.

# What this desk watches. Left out, every cycle is [run].instrument. For evidence, name the
# instruments: a universe discovered from today's volume cannot be replayed on any archive.
# [universe]
# instruments = ["BTCUSDT", "ETHUSDT"]
# max_per_cycle = 1
# max_intents_per_day = 6          # across every instrument, not per one

[execution]
# sandbox keeps the intent here. signals writes it for your own executor. venue sends it, and does
# not start until it is signed: `tra mandate --sign "Your Name" --venue-mode` signs and turns it on
# in one step. It also needs a leverage, max_notional and the keys in .env; on mainnet the ladder
# must also be at CANARY. `tra check` names whatever is missing.
mode = "sandbox"
# acknowledged_by = "your name"
# acknowledged_on = "YYYY-MM-DD"
# max_notional = 200

"""


FIRST_HOUR = """

## Your first hour

This project was written by `tra init --profile autonomous`, so the cycle is already wired. What
is left before it can decide anything:

1. **A model.** Put your key in `.env` (copy `.env.example`). If `tra init` was not given
   `--agent` and `--model`, name the provider and model in `[agents]` in `strategy.toml`.
2. **The mechanism, if you have one.** `[agents].mechanism` is your thesis. The one written here
   lets the measured regime pick the setup; write a narrower one only if you trade one idea, and
   the analyst still never fades a trend for it. The analyst names its own stop and target, held
   inside `[exit]`; `[agents].magnitude_bps` is the edge claimed only when it names no target.
3. **Nothing to download.** `data.toml` already declares the live chains a cycle pulls through;
   a dataset is only for `tra backtest`.

Then `tra check` tells you what is still missing, `tra run` decides once, and
`tra run --every 15m` keeps deciding on each fifteen-minute bar.

The desk is the core: one decision, the exits, the cost rule, the risk gate and execution. The
layers in the "Experiments" block of `strategy.toml` are off because none has been measured against
the core. Turn on one at a time, and keep it only if `tra arena` shows the core with it doing better
than the core without it, out of sample. `tra check` lists the experiments a desk has on.

`docs.bijectivelabs.dev/opensource/boundaries` says what this software will not do, and why.
"""
