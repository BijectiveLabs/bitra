"""`tra record`: a venue's order flow, book, liquidations, derivatives and news kept while they
happen; and `tra record status`, what the records say about how well that went.

The recorder is a process that stays up, like a desk. `--once 30m` runs it for a bounded time, for
a first look or a scheduled job. A desk without a recorder still has flow bars built from the
venue's own archive (`tra data bars`), but no book and no liquidations: those cannot be bought back
later.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_research_agents.data.news import pull_news
from trading_research_agents.data.series import NewsSpec, load_news_map
from trading_research_agents.data.sources.ccxt_client import build
from trading_research_agents.data.sources.config import parse_duration
from trading_research_agents.extras import install_hint
from trading_research_agents.market_data.archive import read_flow_range
from trading_research_agents.market_data.archive_book import read_book_range
from trading_research_agents.market_data.bars import GapState, MarketDataError
from trading_research_agents.market_data.budget import RequestBudget, budget_for
from trading_research_agents.market_data.days import DAY_NS, NS_PER_S, day_of
from trading_research_agents.market_data.record_config import ALL, RecordConfig, load_record_config
from trading_research_agents.market_data.recorder import EVENTS, Recorder
from trading_research_agents.market_data.venues.ccxt_backfill import CcxtBackfill
from trading_research_agents.market_data.venues.ccxt_stream import (
    default_link,
    levels_loop,
    market_of,
    run_streams,
    text_loop,
)
from trading_research_agents.records import read_lines

Report = tuple[dict[str, Any], list[str], int]
Connect = Callable[[str], tuple[Any, Any]]
NEWS = Path("sources") / "news.toml"
HOUR_NS = 3600 * NS_PER_S


def _options(cmd: Any, default: Any) -> None:
    cmd.add_argument(
        "--data-config",
        type=Path,
        default=default if default is argparse.SUPPRESS else Path("data.toml"),
        help="the data.toml whose [record] table says what to keep",
    )
    cmd.add_argument(
        "--out",
        type=Path,
        default=default if default is argparse.SUPPRESS else Path("data"),
        help="the data directory that holds market/",
    )
    cmd.add_argument(
        "--json", action="store_true", default=default if default is argparse.SUPPRESS else False
    )


def add_record_command(top: Any) -> None:
    cmd = top.add_parser(
        "record",
        help="keep a venue's order flow, book, liquidations, derivatives and news while it runs",
    )
    cmd.set_defaults(command="run")
    _options(cmd, None)
    cmd.add_argument("--once", default=None, help="run for this long, such as 30m, then stop")
    sub = cmd.add_subparsers(dest="command", required=False)
    status = sub.add_parser("status", help="coverage, gaps and latency from the records")
    # Settings given before `status` stand unless given again after it.
    _options(status, argparse.SUPPRESS)


def _connect(venue: str) -> tuple[Any, Any]:
    try:
        import ccxt.pro as pro
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise MarketDataError(f"ccxt is not installed; {install_hint('ccxt')}") from exc
    factory = getattr(pro, venue, None)
    rest = build(venue, {"enableRateLimit": True})
    if factory is None or rest is None:
        raise MarketDataError(f"ccxt does not stream {venue!r}")
    return factory({"enableRateLimit": True}), rest


def record_report(args: argparse.Namespace, *, connect: Connect = _connect) -> Report:
    cfg = load_record_config(args.data_config)
    stop_at = None
    if args.once is not None:
        stop_at = time.time_ns() + int(parse_duration(str(args.once)) * NS_PER_S)
    base = args.data_config.resolve().parent
    specs = _feeds(cfg, base)
    pro, rest = connect(cfg.venue)
    asyncio.run(_record(pro, rest, cfg, args.out, specs, base, stop_at))
    root = args.out / "market"
    payload = {
        "venue": cfg.venue,
        "markets": list(cfg.markets),
        "streams": list(cfg.streams),
        "text": [s.name for s in specs],
        "out": str(root),
        "stopped_at": datetime.now(UTC).isoformat(),
    }
    lines = [
        f"recorded {cfg.venue} {', '.join(cfg.markets)} ({', '.join(cfg.streams)}) into {root}",
        "`tra record status` reads what the records say; `tra runs verify --market` checks them",
    ]
    return payload, lines, 0


async def _record(
    pro: Any,
    rest: Any,
    cfg: RecordConfig,
    out: Path,
    specs: Sequence[NewsSpec],
    base: Path,
    stop_at: int | None,
) -> None:
    try:
        await pro.load_markets()
        markets = [market_of(pro, name) for name in cfg.markets]
        now = time.time_ns()
        scales = {m.instrument: m.scales for m in markets}
        feeds = [s.name for s in specs]
        recorder = Recorder(cfg, out, scales=scales, now_ns=now, feeds=feeds)
        link = default_link(cfg.venue, pro, recorder, cfg, stop_at)
        budget = (
            RequestBudget(cfg.requests_per_minute) if cfg.requests_per_minute else budget_for(rest)
        )
        filler = CcxtBackfill(rest, budget=budget)
        extra = [levels_loop(link, filler, markets, now - HOUR_NS)]
        if specs:
            extra.append(text_loop(link, specs, base, pull_news))
        await run_streams(link, markets, extra=extra)
    finally:
        await pro.close()


def _feeds(cfg: RecordConfig, base: Path) -> list[NewsSpec]:
    if not cfg.text:
        return []
    path = base / NEWS
    fetched = [s for s in load_news_map(path) if s.fetch is not None] if path.is_file() else []
    if cfg.text == (ALL,):
        return fetched
    named = {s.name: s for s in fetched}
    unknown = sorted(set(cfg.text) - set(named))
    if unknown:
        raise MarketDataError(
            f"[record].text: {unknown} is not a fetched feed in {path}; have {sorted(named)}"
        )
    return [named[name] for name in cfg.text]


def record_status(args: argparse.Namespace) -> Report:
    cfg = load_record_config(args.data_config)
    root = args.out
    now = time.time_ns()
    since = now - DAY_NS
    events = [
        e
        for e in read_lines(root / "market" / EVENTS)
        if isinstance(e.get("at_ns"), int) and e["at_ns"] >= since
    ]
    markets = {
        name: _market_status(root, cfg.venue, name, events, since, now) for name in cfg.markets
    }
    last = max((e["at_ns"] for e in events), default=None)
    payload = {
        "venue": cfg.venue,
        "last_event": None
        if last is None
        else datetime.fromtimestamp(last / NS_PER_S, UTC).isoformat(),
        "rate_limited_24h": sum(1 for e in events if e.get("event") == "rate_limited"),
        "markets": markets,
    }
    lines = [f"{cfg.venue}: last event {payload['last_event'] or 'none in the last 24 hours'}"]
    for name, m in markets.items():
        lines.append(
            f"  {name}: {m['flow_minutes_24h']} of 1440 flow minutes, {m['book_minutes_24h']} "
            f"book minutes, {m['gap_seconds_24h']} s of gaps, latency p50 {m['latency_p50_ms']} "
            f"ms, max {m['latency_max_ms']} ms"
        )
    return payload, lines, 0


def _market_status(
    root: Path, venue: str, name: str, events: Sequence[dict[str, Any]], since: int, now: int
) -> dict[str, Any]:
    flows = read_flow_range(root, venue, name, day_of(since), day_of(now))
    books = read_book_range(root, venue, name, day_of(since), day_of(now))
    inside = [b for b in flows if since <= b.start_ns < now]
    own = [e for e in events if e.get("instrument") == name]
    latency = [e for e in own if e.get("event") == "latency"]
    return {
        "flow_minutes_24h": sum(1 for b in inside if b.gap is not GapState.MISSING),
        "book_minutes_24h": sum(1 for b in books if since <= b.start_ns < now and b.samples),
        "gap_seconds_24h": sum(
            float(e.get("seconds", 0)) for e in own if e.get("event") == "gap_closed"
        ),
        "latency_p50_ms": latency[-1]["latency_p50_ms"] if latency else None,
        "latency_max_ms": latency[-1]["latency_max_ms"] if latency else None,
    }
