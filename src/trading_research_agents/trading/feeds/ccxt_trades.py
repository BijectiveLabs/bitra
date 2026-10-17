"""Public trades from any venue ccxt streams, for shadow and the live tier. No key is read.

ccxt's streaming side (`ccxt.pro`, shipped inside the `ccxt` package) keeps the socket, the
venue's ping and the subscription; this turns what it returns into the package's `Trade`. It runs
the stream on its own thread with its own event loop, because the runtime is a plain loop over an
iterator, and hands trades across through a queue.

What it refuses to do quietly is the same as any feed here. A trade that does not parse stops the
stream rather than being skipped, because a partial batch leaves a gap that reads as a quiet
market. A trade older than the last one passed on is counted and not passed on, because the
runtime's bars are built in time order. A connection that drops is reopened, and counted.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.extras import install_hint
from trading_research_agents.trading.feed import Feed, FeedError

SIDES = {"buy": Side.BUY, "sell": Side.SELL}
# Trade ids remembered to drop a trade a venue sends twice across a reconnect.
SEEN_LIMIT = 5000


@dataclass
class FeedHealth:
    connects: int = 0
    reconnects: int = 0
    trades: int = 0
    unrecognised: Counter[str] = field(default_factory=Counter)
    rejected: int = 0
    last_message_at: float | None = None


def to_trades(rows: Any, *, venue: str, instrument: str, unit: Decimal) -> list[Trade]:
    """ccxt's unified trades as the package's own, quantities in the base."""
    out: list[Trade] = []
    for row in rows or []:
        try:
            out.append(
                Trade(
                    ts_ns=int(row["timestamp"]) * 1_000_000,
                    venue=venue,
                    instrument=instrument,
                    price=Decimal(str(row["price"])),
                    qty=Decimal(str(row["amount"])) * unit,
                    side=SIDES[str(row["side"]).lower()],
                    trade_id=str(row["id"]),
                )
            )
        except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
            raise FeedError(f"unreadable trade {row!r}: {exc}") from exc
    return out


def _unit(market: Mapping[str, Any]) -> Decimal:
    # Trades on a contract market are counted in contracts; the desk counts the base.
    raw = market.get("contractSize")
    if not market.get("contract") or raw in (None, ""):
        return Decimal(1)
    try:
        unit = Decimal(str(raw))
    except InvalidOperation:
        raise FeedError(f"the venue reports a contract size of {raw!r}") from None
    if not unit.is_finite() or unit <= 0:
        raise FeedError(f"the venue reports a contract size of {raw!r}")
    return unit


class CcxtTradeFeed:
    """Iterate live public trades for one instrument. Reconnects; never fabricates."""

    def __init__(
        self,
        venue: str,
        instrument: str,
        *,
        stop: threading.Event | None = None,
        max_reconnects: int | None = None,
        exchange: Callable[[], Any] | None = None,
    ) -> None:
        if not instrument.strip():
            raise FeedError("a feed names its instrument")
        self.venue = venue.strip().lower()
        self._instrument = instrument
        self.stop = stop or threading.Event()
        self.max_reconnects = max_reconnects
        self._exchange = exchange or self._public_client
        self.health = FeedHealth()
        self.symbol = ""

    @property
    def instrument(self) -> str:
        return self._instrument

    def _public_client(self) -> Any:
        try:
            import ccxt.pro as pro
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise FeedError(f"ccxt is not installed; {install_hint('ccxt')}") from exc
        from trading_research_agents.data.sources.ccxt_client import exchange_class, is_prediction

        # A prediction venue's class is already the async, streaming one.
        factory = exchange_class(self.venue) if is_prediction(self.venue) else None
        factory = factory or getattr(pro, self.venue, None)
        if factory is None:
            raise FeedError(f"ccxt does not stream {self.venue!r}")
        return factory({"enableRateLimit": True})

    def __iter__(self) -> Iterator[Trade]:
        handoff: queue.Queue[Trade | BaseException | None] = queue.Queue(maxsize=10_000)
        worker = threading.Thread(target=self._run, args=(handoff,), daemon=True)
        worker.start()
        last_ts = 0
        try:
            while True:
                try:
                    item = handoff.get(timeout=1.0)
                except queue.Empty:
                    if self.stop.is_set() and not worker.is_alive():
                        return
                    continue
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                if item.ts_ns < last_ts:
                    self.health.unrecognised["older than the last trade"] += 1
                    continue
                last_ts = item.ts_ns
                self.health.trades += 1
                yield item
        finally:
            # A reader that stops reading ends the stream, rather than leave it filling a queue.
            self.stop.set()

    def _run(self, handoff: queue.Queue[Trade | BaseException | None]) -> None:
        try:
            asyncio.run(self._stream(handoff))
        except BaseException as exc:
            handoff.put(exc)
        finally:
            handoff.put(None)

    async def _stream(self, handoff: queue.Queue[Trade | BaseException | None]) -> None:
        import ccxt

        from trading_research_agents.data.sources.resolve import ResolveError, resolve

        seen: dict[str, None] = {}
        attempts = 0
        while not self.stop.is_set():
            if attempts > 0:
                if self.max_reconnects is not None and attempts > self.max_reconnects:
                    raise FeedError(f"gave up after {attempts - 1} reconnects")
                self.health.reconnects += 1
                await asyncio.sleep(min(2.0**attempts, 30.0))
            attempts += 1
            client = self._exchange()
            try:
                await client.load_markets()
                try:
                    self.symbol = resolve(
                        self.instrument,
                        getattr(client, "markets_by_id", None) or {},
                        (getattr(client, "markets", None) or {}).values(),
                    )
                except ResolveError as exc:
                    raise FeedError(str(exc)) from None
                unit = _unit(client.market(self.symbol))
                self.health.connects += 1
                while not self.stop.is_set():
                    try:
                        rows = await asyncio.wait_for(client.watch_trades(self.symbol), 1.0)
                    except TimeoutError:
                        continue
                    self.health.last_message_at = time.monotonic()
                    try:
                        trades = to_trades(
                            rows, venue=self.venue, instrument=self.instrument, unit=unit
                        )
                    except FeedError:
                        self.health.rejected += 1
                        raise
                    for trade in trades:
                        if trade.trade_id in seen:
                            continue
                        seen[trade.trade_id] = None
                        if len(seen) > SEEN_LIMIT:
                            seen.pop(next(iter(seen)))
                        handoff.put(trade)
                return
            except ccxt.NetworkError as exc:
                if self.stop.is_set():
                    return
                self.health.unrecognised[f"disconnect: {type(exc).__name__}"] += 1
            finally:
                await client.close()


class CcxtFeedFactory:
    """The public trade stream of any venue ccxt streams."""

    def __init__(self, venue: str) -> None:
        self.name = venue.strip().lower()

    def __call__(
        self, instrument: str, *, stop: threading.Event, max_reconnects: int | None
    ) -> Feed:
        return CcxtTradeFeed(self.name, instrument, stop=stop, max_reconnects=max_reconnects)
