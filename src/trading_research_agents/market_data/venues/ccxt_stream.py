"""The recorder's link to a venue through ccxt: streams in, events out, a clock ticking.

Per market it watches trades, samples the order book, and watches liquidations where the venue
streams them; every five minutes it backfills the derivatives levels through the same
`CcxtBackfill` the archive path uses, so a level recorded live and one built later from history are
the same object; and it refreshes the declared news feeds on their own interval. A dropped stream
is reported to the recorder as a disconnect at once and as a reconnect when the next message
arrives, with backoff from one second doubling to sixty, plus jitter, in between.

Liquidation sides are venue dialects. Bybit's `S` is the side of the position that was liquidated
(its documentation, read 2026-10-26: "When you receive a Buy update, this means that a long position
has been liquidated"); a venue whose meaning has not been read from its documentation keeps its raw
side and says nothing about which positions were liquidated.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from trading_research_agents.data.schema import Side, Trade, scale_of
from trading_research_agents.data.series import Event, NewsSpec, read_events
from trading_research_agents.data.sources.resolve import resolve
from trading_research_agents.market_data.bars import MarketDataError
from trading_research_agents.market_data.book import BookSample
from trading_research_agents.market_data.deriv import LEVELS, DerivPoint, Series, detail_of
from trading_research_agents.market_data.ports import BudgetStore
from trading_research_agents.market_data.record_config import RecordConfig
from trading_research_agents.market_data.tracks import Scales
from trading_research_agents.market_data.venues.ccxt_backfill import CcxtBackfill

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 60.0
LEVELS_EVERY_S = 300
TICK_S = 1.0
SIDES = {"buy": Side.BUY, "sell": Side.SELL}
LIQUIDATED = {"bybit": {"buy": "long", "sell": "short"}}

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], int]


class RecorderSink(Protocol):
    def on_trades(self, instrument: str, trades: Iterable[Trade], received_ns: int) -> None: ...
    def on_book(self, instrument: str, sample: BookSample) -> None: ...
    def on_points(self, instrument: str, points: Iterable[DerivPoint]) -> None: ...
    def on_text(self, feed: str, events: Iterable[Event], now_ns: int) -> None: ...
    def on_disconnect(
        self,
        instrument: str,
        stream: str,
        since_ns: int,
        reason: str,
        *,
        noticed_ns: int | None = None,
    ) -> None: ...
    def on_reconnect(self, instrument: str, stream: str, at_ns: int) -> None: ...
    def tick(self, now_ns: int) -> None: ...
    def stop(self, now_ns: int) -> None: ...


@dataclass(frozen=True)
class Market:
    instrument: str
    symbol: str
    unit: Decimal
    scales: Scales


def market_of(client: Any, instrument: str) -> Market:
    """The venue's symbol for `instrument`, its contract size in the base, and its scales."""
    markets = getattr(client, "markets", None) or {}
    symbol = resolve(instrument, getattr(client, "markets_by_id", None) or {}, markets.values())
    raw = markets[symbol]
    unit = Decimal(1)
    if raw.get("contract") and raw.get("contractSize") not in (None, ""):
        unit = _decimal(raw["contractSize"], f"{symbol} contract size")
    precision = raw.get("precision") or {}
    price = scale_of(_decimal(precision.get("price"), f"{symbol} price precision"))
    amount = scale_of(_decimal(precision.get("amount"), f"{symbol} amount precision") * unit)
    return Market(instrument, symbol, unit, Scales(price=price, qty=amount))


def to_trades(rows: Sequence[Mapping[str, Any]], *, venue: str, market: Market) -> list[Trade]:
    """ccxt's unified trades as canonical ones, quantities in the base."""
    out: list[Trade] = []
    for row in rows:
        try:
            out.append(
                Trade(
                    ts_ns=int(row["timestamp"]) * NS_PER_MS,
                    venue=venue,
                    instrument=market.instrument,
                    price=_decimal(row["price"], "price"),
                    qty=_decimal(row["amount"], "amount") * market.unit,
                    side=SIDES[str(row["side"]).lower()],
                    trade_id=str(row["id"]),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MarketDataError(f"an unreadable trade from {venue}: {row!r}: {exc}") from exc
    return out


def to_sample(book: Mapping[str, Any], *, market: Market, at_ns: int, levels: int) -> BookSample:
    def side(rows: Any) -> tuple[tuple[Decimal, Decimal], ...]:
        return tuple(
            (_decimal(r[0], "price"), _decimal(r[1], "size") * market.unit) for r in rows[:levels]
        )

    return BookSample(
        at_ns=at_ns, bids=side(book.get("bids") or []), asks=side(book.get("asks") or [])
    )


def to_liquidations(
    rows: Sequence[Mapping[str, Any]], *, venue: str, market: Market, received_ns: int
) -> list[DerivPoint]:
    meaning = LIQUIDATED.get(venue, {})
    out: list[DerivPoint] = []
    for row in rows:
        raw_side = str(row.get("side") or "").lower()
        detail = {"side": raw_side, "price": str(_decimal(row.get("price"), "price"))}
        if raw_side in meaning:
            detail["liquidated"] = meaning[raw_side]
        qty = _decimal(row.get("contracts"), "contracts") * market.unit
        at = int(row["timestamp"]) * NS_PER_MS
        out.append(DerivPoint(Series.LIQUIDATION, at, received_ns, qty, detail_of(**detail)))
    return out


def _decimal(raw: Any, what: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise MarketDataError(f"{what} {raw!r} is not a number") from None
    if not value.is_finite():
        raise MarketDataError(f"{what} {raw!r} is not a number")
    return value


@dataclass
class Link:
    """What the streams share: the venue, the recorder, the clock, and when to stop."""

    venue: str
    client: Any
    recorder: RecorderSink
    config: RecordConfig
    clock: Clock
    sleep: Sleep
    jitter: Callable[[], float]
    stop_at_ns: int | None

    def stopping(self) -> bool:
        return self.stop_at_ns is not None and self.clock() >= self.stop_at_ns


async def _watched(
    link: Link,
    instrument: str,
    stream: str,
    watch: Callable[[], Awaitable[Any]],
    handle: Callable[[Any, int], None],
) -> None:
    """One stream of one market: messages handed on, drops reported, reconnects backed off."""
    down = False
    wait = BACKOFF_START_S
    # A connection that goes silent is noticed only when its keepalive gives up, up to a minute
    # later; what was not heard after the last message was not seen, so the drop starts there.
    heard = link.clock()
    while not link.stopping():
        try:
            message = await watch()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not down:
                why = f"{exc.__class__.__name__}: {exc}"
                link.recorder.on_disconnect(instrument, stream, heard, why, noticed_ns=link.clock())
                down = True
            await link.sleep(wait + link.jitter())
            wait = min(wait * 2, BACKOFF_MAX_S)
            continue
        now = link.clock()
        heard = now
        if down:
            link.recorder.on_reconnect(instrument, stream, now)
            down, wait = False, BACKOFF_START_S
        handle(message, now)


def stream_tasks(link: Link, markets: Sequence[Market]) -> list[Awaitable[None]]:
    """The watching coroutines for every market and stream the venue offers."""
    has = getattr(link.client, "has", None) or {}
    tasks: list[Awaitable[None]] = []
    for market in markets:
        if "trades" in link.config.streams:
            tasks.append(_trades(link, market))
        if "book" in link.config.streams:
            tasks.append(_book(link, market))
        if "liquidations" in link.config.streams and has.get("watchLiquidations") is True:
            tasks.append(_liquidations(link, market))
    return tasks


def _trades(link: Link, market: Market) -> Awaitable[None]:
    def handle(rows: Any, now: int) -> None:
        trades = to_trades(rows or [], venue=link.venue, market=market)
        link.recorder.on_trades(market.instrument, trades, now)

    return _watched(
        link, market.instrument, "trades", lambda: link.client.watch_trades(market.symbol), handle
    )


def _book(link: Link, market: Market) -> Awaitable[None]:
    every_ns = link.config.book_sample_ms * NS_PER_MS
    last = [0]

    def handle(book: Any, now: int) -> None:
        # The venue pushes every change; the archive keeps one look per sampling interval.
        if now - last[0] < every_ns:
            return
        last[0] = now
        sample = to_sample(book, market=market, at_ns=now, levels=link.config.book_levels)
        link.recorder.on_book(market.instrument, sample)

    return _watched(
        link,
        market.instrument,
        "book",
        lambda: link.client.watch_order_book(market.symbol),
        handle,
    )


def _liquidations(link: Link, market: Market) -> Awaitable[None]:
    def handle(rows: Any, now: int) -> None:
        points = to_liquidations(rows or [], venue=link.venue, market=market, received_ns=now)
        link.recorder.on_points(market.instrument, points)

    return _watched(
        link,
        market.instrument,
        "liquidations",
        lambda: link.client.watch_liquidations(market.symbol),
        handle,
    )


async def levels_loop(
    link: Link, filler: CcxtBackfill, markets: Sequence[Market], since_ns: int
) -> None:
    """Every five minutes, the derivatives levels published since the last look, by history."""
    cursors = {(m.instrument, s): since_ns // NS_PER_MS for m in markets for s in LEVELS}
    while not link.stopping():
        now_ms = link.clock() // NS_PER_MS
        for market in markets:
            for series in LEVELS:
                key = (market.instrument, series)
                try:
                    found = await asyncio.to_thread(
                        filler.history, series, market.symbol, cursors[key], now_ms
                    )
                except MarketDataError as exc:
                    # A refused read is asked again from the same point next time, so nothing is
                    # skipped; the recorder hears of it, and the streams carry on.
                    link.recorder.on_disconnect(
                        market.instrument, "levels", now_ms * NS_PER_MS, str(exc)
                    )
                    continue
                link.recorder.on_points(market.instrument, found.points)
                cursors[key] = now_ms + 1
        await link.sleep(LEVELS_EVERY_S)


async def text_loop(link: Link, specs: Sequence[NewsSpec], base: Path, pull: Any) -> None:
    """Each declared feed on its own refresh: fetched into its file, then what is new kept."""
    due = {spec.name: 0 for spec in specs}
    while not link.stopping():
        now = link.clock()
        for spec in specs:
            if now < due[spec.name]:
                continue
            due[spec.name] = now + int(spec.refresh.total_seconds() * NS_PER_S)
            try:
                await asyncio.to_thread(pull, spec, base)
                events = read_events(base / spec.file)
            except Exception as exc:
                link.recorder.on_disconnect(spec.name, "text", now, f"{type(exc).__name__}: {exc}")
                continue
            link.recorder.on_text(spec.name, events, now)
        await link.sleep(TICK_S * 30)


async def run_streams(
    link: Link,
    markets: Sequence[Market],
    *,
    extra: Sequence[Awaitable[None]] = (),
) -> None:
    """Run every stream and the clock until the link's stop time; then stop the recorder."""
    workers = [asyncio.ensure_future(task) for task in (*stream_tasks(link, markets), *extra)]
    try:
        while not link.stopping():
            link.recorder.tick(link.clock())
            await link.sleep(TICK_S)
            failed = [w for w in workers if w.done() and w.exception() is not None]
            if failed:
                raise MarketDataError(f"a recorder task stopped: {failed[0].exception()}")
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        link.recorder.stop(link.clock())


def default_link(
    venue: str, client: Any, recorder: RecorderSink, config: RecordConfig, stop_at_ns: int | None
) -> Link:
    rng = random.Random()  # noqa: S311 - jitter only spreads reconnects; nothing depends on it
    return Link(
        venue=venue,
        client=client,
        recorder=recorder,
        config=config,
        clock=time.time_ns,
        sleep=asyncio.sleep,
        jitter=lambda: rng.uniform(0.0, 0.5),
        stop_at_ns=stop_at_ns,
    )


__all__ = [
    "BudgetStore",
    "Link",
    "Market",
    "RecorderSink",
    "default_link",
    "levels_loop",
    "market_of",
    "run_streams",
    "text_loop",
    "to_liquidations",
    "to_sample",
    "to_trades",
]
