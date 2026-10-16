"""The public trade stream through ccxt, against a double with ccxt.pro's own shapes.

The double answers `watch_trades` from a script, one batch per call, and raises ccxt's own
exceptions where the script says so. A contract market counts in contracts, as one does.
"""

from __future__ import annotations

import asyncio
import threading
from decimal import Decimal
from typing import Any

import pytest

ccxt = pytest.importorskip("ccxt")

from trading_research_agents.data import Side  # noqa: E402
from trading_research_agents.trading.feed import FeedError  # noqa: E402
from trading_research_agents.trading.feeds import UnknownFeedError, live_feed, live_feeds  # noqa: E402
from trading_research_agents.trading.feeds.ccxt_trades import (  # noqa: E402
    CcxtFeedFactory,
    CcxtTradeFeed,
)

SYMBOL = "BTC/USDT:USDT"


def row(
    i: str, ts: int, price: str = "100", amount: str = "3", side: str = "buy"
) -> dict[str, Any]:
    return {"id": i, "timestamp": ts, "price": float(price), "amount": float(amount), "side": side}


class Streamer:
    """One connection's worth of answers. A batch is a list of trades or an exception."""

    def __init__(self, script: list[Any], stop: threading.Event) -> None:
        self.script = script
        self.stop = stop
        self.closed = False
        self.markets = {SYMBOL: self.market(SYMBOL)}
        self.markets_by_id = {"BTC-USDT-SWAP": [self.market(SYMBOL)]}

    def market(self, symbol: str) -> dict[str, Any]:
        return {
            "symbol": SYMBOL,
            "id": "BTC-USDT-SWAP",
            "type": "swap",
            "swap": True,
            "linear": True,
            "contract": True,
            "contractSize": 0.01,
            "base": "BTC",
            "settle": "USDT",
            "active": True,
        }

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    async def watch_trades(self, symbol: str) -> list[dict[str, Any]]:
        if not self.script:
            self.stop.set()
            await asyncio.sleep(0.01)
            return []
        batch = self.script.pop(0)
        if isinstance(batch, BaseException):
            raise batch
        return list(batch)

    async def close(self) -> None:
        self.closed = True


def feed(connections: list[list[Any]], **kwargs: Any) -> tuple[CcxtTradeFeed, list[Streamer]]:
    stop = threading.Event()
    made: list[Streamer] = []

    def connect() -> Streamer:
        made.append(Streamer(connections[len(made)], stop))
        return made[-1]

    return CcxtTradeFeed("okx", "BTCUSDT", stop=stop, exchange=connect, **kwargs), made


def test_trades_arrive_in_the_base_once_each_and_in_time_order() -> None:
    script = [
        [row("a", 1000), row("b", 1001, side="sell")],
        [row("b", 1001, side="sell"), row("c", 1002)],  # b again, as a venue resends across frames
        [row("d", 999)],  # older than what was passed on
    ]
    stream, made = feed([script], max_reconnects=0)
    trades = list(stream)
    assert [t.trade_id for t in trades] == ["a", "b", "c"]
    assert trades[0].qty == Decimal("0.03") and trades[0].instrument == "BTCUSDT"
    assert trades[0].ts_ns == 1000 * 1_000_000 and trades[1].side is Side.SELL
    assert stream.symbol == SYMBOL and stream.health.trades == 3
    assert stream.health.unrecognised["older than the last trade"] == 1
    assert made[0].closed


def test_a_dropped_connection_is_reopened_and_counted() -> None:
    first = [[row("a", 1000)], ccxt.NetworkError("okx connection closed")]
    second = [[row("b", 2000)]]
    stream, made = feed([first, second], max_reconnects=1)
    stream_trades = [t.trade_id for t in stream]
    assert stream_trades == ["a", "b"] and len(made) == 2
    assert stream.health.reconnects == 1 and stream.health.connects == 2
    assert stream.health.unrecognised["disconnect: NetworkError"] == 1


def test_a_stream_that_keeps_dropping_gives_up_by_name() -> None:
    stream, _ = feed([[ccxt.NetworkError("down")], [ccxt.NetworkError("down")]], max_reconnects=0)
    with pytest.raises(FeedError, match="gave up after 0 reconnects"):
        list(stream)


def test_an_unreadable_trade_stops_the_stream_rather_than_leaving_a_gap() -> None:
    stream, _ = feed(
        [[[{"id": "x", "timestamp": 1, "price": "a lot", "amount": 1, "side": "buy"}]]]
    )
    with pytest.raises(FeedError, match="unreadable trade"):
        list(stream)
    assert stream.health.rejected == 1


def test_every_venue_ccxt_streams_is_a_feed() -> None:
    assert {"bybit", "okx", "binance"} <= set(live_feeds())
    factory = live_feed("BYBIT")
    assert isinstance(factory, CcxtFeedFactory) and factory.name == "bybit"
    built = factory("BTCUSDT", stop=threading.Event(), max_reconnects=0)
    assert isinstance(built, CcxtTradeFeed) and built.instrument == "BTCUSDT"
    with pytest.raises(UnknownFeedError, match="not a venue ccxt streams"):
        live_feed("no-such-feed")
