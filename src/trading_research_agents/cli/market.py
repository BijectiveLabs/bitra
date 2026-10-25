"""`tra data bars`: one-minute flow bars from a venue's trade archive and the market's
derivatives history, into the verified archive; and the check behind `tra runs verify --market`.

The work is here rather than in main so the command line stays a thin layer: each function
returns what it found and the caller prints it, as text or as JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from trading_research_agents.data.parquet import read_scales, read_trades
from trading_research_agents.data.sources.ccxt_client import build
from trading_research_agents.data.sources.resolve import ResolveError, resolve
from trading_research_agents.data.venues import archive_venue
from trading_research_agents.extras import install_hint
from trading_research_agents.market_data.archive import (
    BUILDER_VERSION,
    FLOW,
    ArchiveError,
    SourceFile,
    day_file,
    read_deriv_day,
    verified,
    verify_archive,
    write_deriv_day,
    write_flow_day,
)
from trading_research_agents.market_data.bars import FlowBar, MarketDataError
from trading_research_agents.market_data.budget import budget_for
from trading_research_agents.market_data.builder import FlowBarBuilder
from trading_research_agents.market_data.days import DAY_NS, day_of, midnight_ns
from trading_research_agents.market_data.deriv import LEVELS, DerivPoint, Series
from trading_research_agents.market_data.gaps import GapMark, record_gap
from trading_research_agents.market_data.venues.ccxt_backfill import DERIV_VENUES, CcxtBackfill
from trading_research_agents.records import sha256_of

MAX_DAYS = 366
Fetch = Callable[[argparse.Namespace], int]
Report = tuple[dict[str, Any], list[str], int]


def _positive_decimal(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive amount")
    return value


def add_bars_command(data: Any, venues: Sequence[str]) -> None:
    cmd = data.add_parser(
        "bars", help="one-minute flow bars from the venue archive, and derivatives history"
    )
    cmd.add_argument("instrument")
    cmd.add_argument("start", type=date.fromisoformat, help="first UTC day, YYYY-MM-DD")
    cmd.add_argument("--to", dest="end", type=date.fromisoformat, default=None, help="last UTC day")
    cmd.add_argument("--venue", choices=venues, help="defaults to the venue in strategy.toml")
    cmd.add_argument("--config", type=Path, default=Path("strategy.toml"))
    cmd.add_argument("--out", type=Path, default=Path("data"))
    cmd.add_argument(
        "--large-trade-quote",
        type=_positive_decimal,
        default=None,
        help="a trade of at least this notional counts as large",
    )
    cmd.add_argument("--no-deriv", action="store_true", help="flow bars only")
    cmd.add_argument("--json", action="store_true")


@dataclass
class _Run:
    built: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    fetched: list[str] = field(default_factory=list)


def data_bars(args: argparse.Namespace, *, fetch: Fetch) -> Report:
    """Build every requested day that is not already in the archive as it would be built now."""
    venue_name = args.venue or _venue_from(args.config)
    days = _days(args.start, args.end or args.start)
    venue = archive_venue(venue_name)
    run = _Run()
    for when, covered in _units(venue.cadence, days):
        wanted = [d for d in covered if not _current(args, venue_name, d, run)]
        if not wanted:
            continue
        trades = _trades_file(args, venue_name, when, wanted, fetch, run)
        if trades is not None:
            _build_days(args, venue_name, trades, wanted, run)
    derivatives = {"skipped": "--no-deriv"} if args.no_deriv else _backfill(args, venue_name, days)
    payload: dict[str, Any] = {
        "venue": venue_name,
        "instrument": args.instrument,
        "built": run.built,
        "kept": run.kept,
        "failed": run.failed,
        "derivatives": derivatives,
    }
    code = 1 if run.failed or any(str(v).startswith("failed") for v in derivatives.values()) else 0
    return payload, _lines(payload, run), code


def verify_market(root: Path) -> Report:
    check = verify_archive(root)
    lines = [f"{check.days} market day file(s) under {root / 'market'} checked"]
    lines += [f"FAILED {problem}" for problem in check.problems]
    if not check.problems:
        lines.append("every day file is what its manifest says")
    payload = {"days": check.days, "problems": list(check.problems)}
    return payload, lines, 1 if check.problems else 0


def _venue_from(config: Path) -> str:
    if config.is_file():
        raw = tomllib.loads(config.read_text(encoding="utf-8"))
        named = str(raw.get("run", {}).get("venue_key", "")).partition("/")[0]
        if named:
            return named
    raise ValueError("no strategy.toml here to take the venue from; pass --venue")


def _days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"--to {end} is before {start}")
    count = (end - start).days + 1
    if count > MAX_DAYS:
        raise ValueError(f"{count} days asked; at most {MAX_DAYS} at a time")
    return [start + timedelta(days=i) for i in range(count)]


def _units(cadence: str, days: list[date]) -> list[tuple[str, list[date]]]:
    """The venue's archive files covering `days`: one per day, or one per month."""
    if cadence == "daily":
        return [(d.isoformat(), [d]) for d in days]
    months: dict[str, list[date]] = {}
    for d in days:
        months.setdefault(f"{d.year:04d}-{d.month:02d}", []).append(d)
    return list(months.items())


def _current(args: argparse.Namespace, venue: str, day: date, run: _Run) -> bool:
    """Whether the day is already archived, verified, by this builder, with the same options."""
    path = day_file(args.out, venue, args.instrument, FLOW, day)
    if not path.is_file():
        return False
    try:
        manifest = verified(path)
    except ArchiveError:
        return False
    wanted = None if args.large_trade_quote is None else str(args.large_trade_quote)
    if manifest.builder != BUILDER_VERSION or manifest.large_trade_quote != wanted:
        return False
    run.kept.append(day.isoformat())
    return True


def _trades_file(
    args: argparse.Namespace, venue: str, when: str, days: list[date], fetch: Fetch, run: _Run
) -> Path | None:
    path: Path = args.out / f"{args.instrument}-{when}.parquet"
    if path.is_file():
        return path
    asked = argparse.Namespace(
        instrument=args.instrument,
        when=when,
        venue=venue,
        config=args.config,
        out=args.out,
        max_gap=None,
        json=False,
    )
    said = io.StringIO()
    try:
        with contextlib.redirect_stdout(said):
            code = fetch(asked)
    except Exception as exc:
        _fail(args, venue, days, f"archive unavailable: {exc.__class__.__name__}: {exc}", run)
        return None
    run.fetched.extend(line for line in said.getvalue().splitlines() if line.strip())
    if code != 0 or not path.is_file():
        _fail(args, venue, days, f"archive refused by the data audit (exit {code})", run)
        return None
    return path


def _build_days(
    args: argparse.Namespace, venue: str, trades: Path, days: list[date], run: _Run
) -> None:
    price_scale, qty_scale = read_scales(trades)
    source = SourceFile(trades.name, sha256_of(trades))
    builders: dict[date, FlowBarBuilder] = {}
    bars: dict[date, list[FlowBar]] = {d: [] for d in days}
    for trade in read_trades(trades):
        day = day_of(trade.ts_ns)
        if day not in bars:
            continue
        made = builders.get(day)
        if made is None:
            made = builders[day] = FlowBarBuilder(
                venue=venue,
                instrument=args.instrument,
                source=f"archive:{venue}",
                start_ns=midnight_ns(day),
                large_trade_quote=args.large_trade_quote,
            )
        bars[day].extend(made.push(trade))
    for day in days:
        made = builders.get(day)
        if made is None:
            _fail(args, venue, [day], "the archive holds no trade for this day", run)
            continue
        closed = bars[day] + made.finish(midnight_ns(day) + DAY_NS)
        late = made.take_late()
        closed = [bar.with_late(late.get(bar.start_ns, 0)) for bar in closed]
        write_flow_day(
            closed,
            args.out,
            day=day,
            price_scale=price_scale,
            qty_scale=qty_scale,
            sources=(source,),
            large_trade_quote=args.large_trade_quote,
        )
        run.built.append(day.isoformat())


def _fail(args: argparse.Namespace, venue: str, days: list[date], why: str, run: _Run) -> None:
    for day in days:
        run.failed[day.isoformat()] = why
        start = midnight_ns(day)
        record_gap(args.out, GapMark(venue, args.instrument, FLOW, start, start + DAY_NS, why))


def _backfill(args: argparse.Namespace, venue: str, days: list[date]) -> dict[str, str]:
    ccxt_id = DERIV_VENUES.get(venue)
    if ccxt_id is None:
        return {"skipped": f"the {venue} archive is not a perpetual market; nothing to backfill"}
    wanted = [d for d in days if not _deriv_complete(args, venue, d)]
    kept = len(days) - len(wanted)
    report = {"kept": f"{kept} day(s) already complete"} if kept else {}
    if not wanted:
        return report
    # Public data only: no key, and ccxt's own limiter under this process's budget.
    client = build(ccxt_id, {"enableRateLimit": True})
    if client is None:
        why = f"ccxt is not installed or has no venue named {ccxt_id!r}; {install_hint('ccxt')}"
        return {**report, "skipped": why}
    try:
        symbol = _symbol(client, args.instrument)
    except (ResolveError, MarketDataError) as exc:
        return {**report, "skipped": str(exc)}
    filler = CcxtBackfill(client, budget=budget_for(client))
    start_ms = midnight_ns(wanted[0]) // 1_000_000
    end_ms = (midnight_ns(wanted[-1]) + DAY_NS) // 1_000_000 - 1
    by_day: dict[date, list[DerivPoint]] = {d: [] for d in wanted}
    for series in LEVELS:
        report[series.value] = _one_series(filler, series, symbol, start_ms, end_ms, by_day)
    if any(said.startswith("failed") for said in report.values()):
        # A day without one of its series would read as a day the venue had nothing for.
        return report
    sources = (SourceFile(f"ccxt:{ccxt_id}", None),)
    for day, points in by_day.items():
        write_deriv_day(
            points, args.out, venue=venue, instrument=args.instrument, day=day, sources=sources
        )
    return report


def _deriv_complete(args: argparse.Namespace, venue: str, day: date) -> bool:
    """Whether the day's derivatives were asked for, verified, after the day had ended.

    A day asked for while it was still running holds only its first hours, and a day the venue
    answered with nothing is cheap to ask again; either is asked again.
    """
    try:
        points = read_deriv_day(args.out, venue, args.instrument, day)
    except ArchiveError:
        return False
    end = midnight_ns(day) + DAY_NS
    return bool(points) and min(p.received_ns for p in points) >= end


def _one_series(
    filler: CcxtBackfill,
    series: Series,
    symbol: str,
    start_ms: int,
    end_ms: int,
    by_day: dict[date, list[DerivPoint]],
) -> str:
    try:
        got = filler.history(series, symbol, start_ms, end_ms)
    except MarketDataError as exc:
        return f"failed: {exc}"
    if not got.supported:
        return "unsupported by the venue"
    kept = 0
    for point in got.points:
        day = day_of(point.as_of_ns)
        if day in by_day:
            by_day[day].append(point)
            kept += 1
    dropped = f", {got.dropped} row(s) without a value" if got.dropped else ""
    return f"{kept} point(s) in {got.pages} page(s){dropped}"


def _symbol(client: Any, instrument: str) -> str:
    try:
        client.load_markets()
    except Exception as exc:
        raise MarketDataError(f"cannot load the venue's markets: {exc}") from exc
    markets = getattr(client, "markets", None) or {}
    return resolve(instrument, getattr(client, "markets_by_id", None) or {}, markets.values())


def _lines(payload: dict[str, Any], run: _Run) -> list[str]:
    lines = list(run.fetched)
    head = f"{payload['venue']} {payload['instrument']}"
    lines.append(f"{head}: {len(run.built)} day(s) built, {len(run.kept)} already current")
    lines += [f"FAILED {day}: {why}" for day, why in sorted(run.failed.items())]
    for name, said in payload["derivatives"].items():
        lines.append(f"  {name:<14} {said}")
    return lines
