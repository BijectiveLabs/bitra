"""`tra arena`: every row replayed over the market archive, settled the same way, side by side.

Settings come from the `[arena]` table of an `arena.toml` when one is named, with flags on top.
Spread and slippage are declared, never assumed: the arena refuses to run without them, and the
report labels them declared beside the fees it read and the funding it measured.
"""

from __future__ import annotations

import argparse
import difflib
import tomllib
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.cli.market import venue_from
from trading_research_agents.envelope.load import load_envelope
from trading_research_agents.envelope.model import Channel, Envelope, EnvelopeError
from trading_research_agents.evaluation.arena import ArenaConfig, ArenaResult, run_arena
from trading_research_agents.evaluation.records import write_arena
from trading_research_agents.evaluation.rows import RANDOM, V1_RECORD
from trading_research_agents.evaluation.settle import Costs

Report = tuple[dict[str, Any], list[str], int]
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400}
HELP = {
    "instruments": "comma-separated, such as BTCUSDT,ETHUSDT",
    "from": "first UTC day of the window, YYYY-MM-DD",
    "to": "last UTC day of the window",
    "rows": "comma-separated; default buy_hold,momentum,fade,random; add v1_record for the record",
    "interval": "decision interval (default 1h)",
    "context_interval": "the second timeframe in every view (default 4h)",
    "context": "markets every view reads beside its own (default BTCUSDT)",
    "venue": "defaults to the venue in strategy.toml",
    "data": "the data directory that holds market/ (default data)",
    "envelope": "the envelope with the channel's fees (default envelope.toml)",
    "channel": "venue/name of the channel; default the venue's taker-taker channel",
    "spread_bps": "the full quoted spread you pay, bps per round trip",
    "slippage_bps": "slippage, bps per round trip",
    "funding": "archive (measured, the default) or none, for a market without funding",
    "seed": "seed of the random row (default 7)",
    "runs": "where the v1 record is read from (default runs/cycles)",
    "v1_hold": "how long a v1 decision is held (default 4h)",
    "fit_days": "walk-forward fit window, in days",
    "test_days": "walk-forward test window, in days",
    "out": "where the run is written (default runs/arena)",
}
KEYS = tuple(HELP)
DESTS = {"from": "start", "to": "end"}
DEFAULTS: dict[str, Any] = {
    "rows": "buy_hold,momentum,fade,random",
    "interval": "1h",
    "context_interval": "4h",
    "context": "BTCUSDT",
    "data": "data",
    "envelope": "envelope.toml",
    "funding": "archive",
    "seed": 7,
    "runs": "runs/cycles",
    "v1_hold": "4h",
    "out": "runs/arena",
}
REQUIRED = ("instruments", "from", "to", "spread_bps", "slippage_bps")


def add_arena_command(top: Any) -> None:
    cmd = top.add_parser(
        "arena", help="every row replayed over the market archive and settled the same way"
    )
    cmd.set_defaults(command="arena")
    cmd.add_argument(
        "--config", type=Path, default=None, help="an arena.toml with an [arena] table"
    )
    cmd.add_argument("--strategy", type=Path, default=Path("strategy.toml"))
    for key, text in HELP.items():
        cmd.add_argument(f"--{key.replace('_', '-')}", dest=DESTS.get(key, key), help=text)
    cmd.add_argument(
        "--require-verdict",
        action="store_true",
        help="exit 1 when a row's sample is too small or the row is contaminated",
    )
    cmd.add_argument("--json", action="store_true")


def arena_report(args: argparse.Namespace) -> Report:
    settings = _settings(args)
    missing = [f"--{k.replace('_', '-')}" for k in REQUIRED if settings.get(k) in (None, "")]
    if missing:
        raise ValueError(
            f"tra arena needs {', '.join(missing)}, on the command line or in [arena]; "
            "spread and slippage are declared, never assumed"
        )
    venue = str(settings.get("venue") or venue_from(args.strategy))
    envelope = load_envelope(Path(settings["envelope"]))
    costs = Costs(
        _channel(envelope, venue, settings.get("channel")),
        _bps(settings["spread_bps"], "spread_bps"),
        _bps(settings["slippage_bps"], "slippage_bps"),
    )
    config = _config(settings, venue, costs)
    cycles = Path(settings["runs"]) if V1_RECORD in config.rows else None
    result = run_arena(config, root=Path(settings["data"]), cycles=cycles)
    folder = write_arena(result, Path(settings["out"]))
    payload = {**result.record(), "written_to": str(folder)}
    unproven = [r for r in result.rows if r["row"] != RANDOM and _unproven(str(r["verdict"]))]
    code = 1 if args.require_verdict and unproven else 0
    return payload, _lines(result, folder), code


def _unproven(verdict: str) -> bool:
    return verdict == "sample too small" or verdict.startswith("contaminated")


def _settings(args: argparse.Namespace) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    if args.config is not None:
        merged.update(_from_file(args.config))
    for key in KEYS:
        value = getattr(args, DESTS.get(key, key))
        if value is not None:
            merged[key] = value
    return merged


def _from_file(path: Path) -> dict[str, Any]:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    extra = sorted(set(raw) - {"arena"})
    if extra:
        raise ValueError(f"{path.name}: only [arena] is read; found [{'], ['.join(extra)}]")
    table = dict(raw.get("arena") or {})
    unknown = sorted(set(table) - set(KEYS))
    if unknown:
        hints = [
            f"{name} (did you mean {close[0]}?)" if close else name
            for name in unknown
            for close in [difflib.get_close_matches(name, KEYS, n=1)]
        ]
        raise ValueError(f"{path.name}: [arena] does not read {', '.join(hints)}")
    return table


def _config(settings: dict[str, Any], venue: str, costs: Costs) -> ArenaConfig:
    return ArenaConfig(
        venue=venue,
        instruments=_names(settings["instruments"]),
        start=_day(settings["from"]),
        end=_day(settings["to"]),
        rows=_names(settings["rows"]),
        costs=costs,
        interval_s=_seconds(settings["interval"]),
        context_interval_s=_seconds(settings["context_interval"]),
        context=_names(settings["context"]),
        seed=int(settings["seed"]),
        funding=str(settings["funding"]),
        v1_hold_s=_seconds(settings["v1_hold"]),
        fit_days=_count(settings.get("fit_days")),
        test_days=_count(settings.get("test_days")),
    )


def _names(value: Any) -> tuple[str, ...]:
    parts = value if isinstance(value, list | tuple) else str(value).split(",")
    return tuple(str(p).strip() for p in parts if str(p).strip())


def _day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _seconds(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if len(text) < 2 or text[-1] not in UNITS or not text[:-1].isdigit():
        raise ValueError(f"{value!r} is not a duration such as 15m, 1h or 4h")
    return int(text[:-1]) * UNITS[text[-1]]


def _count(value: Any) -> int | None:
    return None if value in (None, "") else int(value)


def _bps(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} {value!r} is not a number of basis points") from None


def _channel(envelope: Envelope, venue: str, key: Any) -> Channel:
    keys = ", ".join(c.key for c in envelope.channels)
    if key:
        for channel in envelope.channels:
            if channel.key == str(key):
                return channel
        raise EnvelopeError(f"no channel {key!r} in the envelope; declared: {keys}")
    takers = [c for c in envelope.channels if c.venue == venue and c.roles == ("taker", "taker")]
    if len(takers) == 1:
        return takers[0]
    raise EnvelopeError(
        f"name the channel with --channel: {venue} has no taker-taker channel among {keys}"
    )


def _cell(value: Any) -> str:
    return "-" if value is None else str(value)


def _row_line(row: dict[str, Any]) -> str:
    against = row["against_random"]
    interval = ""
    if against is not None:
        interval = f"{against['difference_bps']} [{against['low_bps']}, {against['high_bps']}], "
    figures = (
        f"{row['row']:<12}{row['ideas']:>7}{row['independent_bets']:>6}"
        f"{_cell(row['mean_net_bps']):>9}{_cell(row['median_net_bps']):>9}"
        f"{_cell(row['hit_rate']):>7}{_cell(row['mean_r']):>8}"
        f"{_cell(row['sum_net_bps']):>10}{_cell(row['sharpe_daily']):>8}"
    )
    return f"{figures}  {interval}{row['verdict']}"


def _lines(result: ArenaResult, folder: Path) -> list[str]:
    config = result.config
    costs = config.costs.record()
    lines = [
        f"arena {config.venue} {', '.join(config.instruments)}: {config.start} to {config.end}, "
        f"a decision every {config.interval_s // 60} minutes",
        f"costs: {costs['channel']} fees {costs['entry_fee_bps']} + {costs['exit_fee_bps']} bps; "
        f"spread {costs['spread_bps']} and slippage {costs['slippage_bps']} bps declared; "
        f"funding {config.funding}",
        f"{'row':<12}{'ideas':>7}{'bets':>6}{'mean':>9}{'median':>9}{'hit':>7}"
        f"{'mean R':>8}{'sum':>10}{'sharpe':>8}  against random [95%], verdict",
    ]
    lines += [_row_line(row) for row in result.rows]
    for name, market in result.coverage["markets"].items():
        lines.append(
            f"  {name}: {market['minutes']} minutes, {market['missing_minutes']} not seen, "
            f"{market['partial_minutes']} partial"
        )
    absent = sorted(result.coverage["features_absent"].items(), key=lambda kv: -kv[1]["absent"])
    for feature, entry in absent[:5]:
        lines.append(f"  {feature} absent {entry['absent']} time(s): {entry['reason']}")
    lines.append(f"written to {folder}")
    return lines
