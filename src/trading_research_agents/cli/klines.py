"""`tra data klines`: a market's hour candles and its funding history, for arena runs of years.

Public data only, with no key. The candles must carry the taker split and the trade count; the
command refuses a venue whose candles do not, rather than store a split nobody measured. The run
writes one file per market and replaces it whole, so a second run over a longer window extends
the history by asking for all of it again.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trading_research_agents.data.sources.ccxt_client import build
from trading_research_agents.data.sources.resolve import ResolveError, resolve
from trading_research_agents.extras import install_hint
from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.budget import budget_for
from trading_research_agents.market_data.days import DAY_NS, midnight_ns
from trading_research_agents.market_data.deriv import Series
from trading_research_agents.market_data.klines import (
    bars_from_klines,
    funding_on_the_second,
    write_klines,
)
from trading_research_agents.market_data.venues.ccxt_backfill import CcxtBackfill
from trading_research_agents.market_data.venues.ccxt_klines import (
    DEFAULT_VENUE,
    ENDPOINTS,
    fetch_kline_rows,
)

Report = tuple[dict[str, Any], list[str], int]
ClientFor = Callable[[str], Any]


def add_klines_command(data: Any) -> None:
    cmd = data.add_parser(
        "klines", help="hour candles with the taker split, and funding, for long arena runs"
    )
    cmd.add_argument("instrument")
    cmd.add_argument("start", type=date.fromisoformat, help="first UTC day, YYYY-MM-DD")
    cmd.add_argument("--to", dest="end", type=date.fromisoformat, default=None, help="last UTC day")
    cmd.add_argument(
        "--venue",
        default=DEFAULT_VENUE,
        help=f"a venue whose candles carry the taker split: {', '.join(sorted(ENDPOINTS))}",
    )
    cmd.add_argument("--out", type=Path, default=Path("data"))
    cmd.add_argument("--json", action="store_true")


def _public_client(venue: str) -> Any:
    return build(venue, {"enableRateLimit": True})


def data_klines(args: argparse.Namespace, *, client_for: ClientFor = _public_client) -> Report:
    """Fetch the window's candles and funding, and store them as one market's kline history."""
    end_day = args.end or date.today() - timedelta(days=1)
    if end_day < args.start:
        raise ValueError(f"--to {end_day} is before {args.start}")
    if args.venue not in ENDPOINTS:
        raise ValueError(
            f"{args.venue!r} candles do not carry the taker split; use one of {sorted(ENDPOINTS)}"
        )
    client = client_for(args.venue)
    if client is None:
        raise ValueError(f"ccxt has no venue named {args.venue!r}; {install_hint('ccxt')}")
    start_ms = midnight_ns(args.start) // 1_000_000
    end_ms = (midnight_ns(end_day) + DAY_NS) // 1_000_000 - 1
    budget = budget_for(client)
    try:
        symbol = _symbol(client, args.instrument)
        rows = fetch_kline_rows(client, args.instrument, start_ms, end_ms, budget=budget)
        funding = CcxtBackfill(client, budget=budget).history(
            Series.FUNDING, symbol, start_ms, end_ms
        )
    except (ResolveError, MarketDataError) as exc:
        raise ValueError(str(exc)) from exc
    bars = bars_from_klines(rows, venue=args.venue, instrument=args.instrument)
    manifest = write_klines(args.out, bars, funding_on_the_second(funding.points))
    payload = {**manifest, "from": args.start.isoformat(), "to": end_day.isoformat()}
    lines = [
        f"{args.venue} {args.instrument}: {manifest['rows']} hours, "
        f"{manifest['missing_rows']} not returned, {manifest['funding_rows']} funding settlements",
        f"wrote {args.out / 'klines' / args.venue / args.instrument}",
    ]
    return payload, lines, 0


def _symbol(client: Any, instrument: str) -> str:
    client.load_markets()
    markets = getattr(client, "markets", None) or {}
    return resolve(instrument, getattr(client, "markets_by_id", None) or {}, markets.values())
