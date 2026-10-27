"""What a venue can do through ccxt, and setting a desk up on one: data, orders, exits, account
reads, fees.
"""

from __future__ import annotations

import argparse
import tomllib
from decimal import Decimal
from typing import Any

from trading_research_agents.cli.common import (
    CLEARED,
    REFUSED,
    _environment,
    _environment_words,
    emit,
)
from trading_research_agents.cli.fees import _bps, _read_fees


def show_venues(args: argparse.Namespace) -> int:
    """What a venue can actually do, asked of ccxt rather than promised in a README."""
    from trading_research_agents.data.sources.capability import (
        VENUE_OVERVIEW,
        CapabilityError,
        capability_of,
    )
    from trading_research_agents.trading.dispatch import credentials_for

    names = list(args.venue)
    if not names and args.config.is_file():
        raw = tomllib.loads(args.config.read_text(encoding="utf-8"))
        declared = str(raw.get("run", {}).get("venue_key", "")).partition("/")[0]
        names = [declared] if declared else []
    overview = not names
    if overview:
        # With nothing named and no project, the useful answer is the list a person picks from.
        names = list(VENUE_OVERVIEW)

    found = []
    lines: list[str] = []
    refused = False
    for name in names:
        try:
            able = capability_of(name)
        except CapabilityError as exc:
            lines.append(f"{name}: {exc}")
            refused = True
            if overview:
                break
            continue
        found.append(able.record())
        if overview:
            lines.append(f"{able.venue:<14} {able.verdict()}")
            continue
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
                for what in (
                    "create_order",
                    "attached_exits",
                    "position",
                    "open_orders",
                    "cancel",
                    "order_status",
                    "leverage",
                    "fills",
                )
            )
        )
        lines.append(f"  practice    {able.practice}")
        keys = list(credentials_for(able.venue).values())
        lines.append(f"  keys        {', '.join(keys)}")
        lines.append(f"  verdict     {able.verdict()}")
        if args.setup:
            fees, said = _account_fees(args, able, keys)
            snippet = venue_setup(able, keys, fees=fees, fees_note=said)
            found[-1]["setup"] = snippet
            lines.extend(["", *snippet.splitlines(), ""])
    if overview and not refused:
        lines.append("`tra venues NAME` says what one can do; `--setup` writes the blocks to paste")
    emit({"venues": found}, args.json, lines)
    return REFUSED if refused else CLEARED


def _account_fees(
    args: argparse.Namespace, able: Any, keys: list[str]
) -> tuple[tuple[Decimal, Decimal] | None, str]:
    """The account's own fees, when the keys are at hand; otherwise why not. Read only."""
    if able.level == "data":
        return None, ""
    env = _environment(args.env_file, beside=args.config.resolve().parent)
    environment = args.environment or (
        "demo" if able.demo else "testnet" if able.testnet else "mainnet"
    )
    fees, said = _read_fees(able.venue, keys, env, environment, args.instrument)
    return fees, said if fees is not None else f"not read: {said}; fill the fees yourself"


def venue_setup(
    able: Any,
    keys: list[str],
    *,
    fees: tuple[Decimal, Decimal] | None = None,
    fees_note: str = "",
) -> str:
    """The blocks an operator pastes to trade on a venue, shaped by what the venue can do.

    Fees are the account's own, read when the keys are at hand. Otherwise they are left blank on
    purpose: a fee is the largest figure in the cost gate, and a placeholder number would price
    every trade at a rate nobody checked. A blank one makes the file refuse to load, and says why.
    """
    from trading_research_agents.trading.venues.exits import exit_reads

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
    maker, taker = ("", "") if fees is None else (_bps(fees[0]), _bps(fees[1]))
    out += [
        "",
        "# 1. envelope.toml: what a round trip costs here, in bps (0.055% is 5.5).",
        *([f"#    Fees {fees_note}."] if fees_note else []),
        "#    Until they are written, the file will not load.",
        "[[channel]]",
        f'venue = "{name}"',
        'name = "taker-taker"',
        f"entry_fee_bps = {taker}" + ("" if fees else "                  # your taker fee"),
        f"exit_fee_bps = {taker}" + ("" if fees else "                   # your taker fee"),
        "",
        "[[channel]]",
        f'venue = "{name}"',
        'name = "maker-taker"',
        f"entry_fee_bps = {maker}" + ("" if fees else "                  # your maker fee"),
        f"exit_fee_bps = {taker}" + ("" if fees else "                   # your taker fee"),
        "",
        f'# 2. data.toml: put "ccxt:{name}" first in the chain of {", ".join(serves)}.',
        "#    Each instrument may need its ccxt symbol under [data].symbols.",
        "",
        "# 3. strategy.toml. Sizes use the venue's own step; [run].size_step only overrides it.",
        "[run]",
        f'venue_key = "{name}/taker-taker"',
        "",
        "[execution]",
        f'venue = "{name}"',
        f'environment = "{environment}"      # where orders go; {_environment_words(able)}',
        "leverage = 3                     # yours to choose; applied at the venue and read back",
        'margin_mode = "isolated"         # isolated keeps one position from drawing on the rest',
    ]
    if able.level == "exits-none" and able.can_place_exits and exit_reads(name):
        out += [
            'exits = "separate"               # no stop with the entry here; both go out as',
            "                                 # reduce-only orders right after the fill",
        ]
    elif able.level == "exits-none":
        out += [
            'exits = "none"                   # this venue cannot attach a stop to the entry;',
            "                                 # you carry the stop and take profit yourself",
        ]
    if environment == "mainnet":
        out.append("# no practice environment through ccxt: the first order here is real money")
    out += ["", "# 4. .env beside the config, never in a file you commit", *(f"{k}=" for k in keys)]
    out += ["", f"# Then: tra check, and tra keys --probe to prove the keys reach {name}."]
    return "\n".join(out)
