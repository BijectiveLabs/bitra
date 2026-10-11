"""The live feed against a local WebSocket server that speaks the venue's dialect. No network."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.data import Side
from trading_research_agents.trading.feed import FeedError
from trading_research_agents.trading.feeds import UnknownFeedError, live_feed, live_feeds
from trading_research_agents.trading.feeds.bybit import BybitPublicFeed, parse_public_trade

pytest.importorskip("websockets")

from websockets.sync.server import ServerConnection, serve

ROW: dict[str, Any] = {
    "T": 1672304486865,
    "s": "BTCUSDT",
    "S": "Buy",
    "v": "0.001",
    "p": "16578.50",
    "L": "PlusTick",
    "i": "20f43950-d8dd-5b31-9112-a178eb6023af",
    "BT": False,
}
FRAME: dict[str, Any] = {
    "topic": "publicTrade.BTCUSDT",
    "type": "snapshot",
    "ts": 1672304486868,
    "data": [ROW],
}


def test_the_documented_frame_parses_exactly() -> None:
    trades = parse_public_trade(json.dumps(FRAME), instrument="BTCUSDT")
    assert trades is not None and len(trades) == 1
    t = trades[0]
    assert t.ts_ns == 1672304486865 * 1_000_000
    assert t.price == Decimal("16578.50") and t.qty == Decimal("0.001")
    assert t.side is Side.BUY and t.trade_id == "20f43950-d8dd-5b31-9112-a178eb6023af"
    assert t.venue == "bybit" and t.instrument == "BTCUSDT"
    assert parse_public_trade(json.dumps({"op": "pong"}), instrument="BTCUSDT") is None
    assert (
        parse_public_trade(
            json.dumps({**FRAME, "topic": "publicTrade.ETHUSDT"}), instrument="BTCUSDT"
        )
        is None
    )
    with pytest.raises(FeedError, match="not JSON"):
        parse_public_trade("{", instrument="BTCUSDT")
    bad = {**FRAME, "data": [{**ROW, "p": "not a price"}]}
    with pytest.raises(FeedError, match="unreadable trade row"):
        parse_public_trade(json.dumps(bad), instrument="BTCUSDT")
    with pytest.raises(FeedError, match="only wss"):
        BybitPublicFeed("BTCUSDT", url="ws://example.com/stream")


@pytest.fixture
def server() -> Iterator[tuple[str, list[str]]]:
    received: list[str] = []

    def handler(ws: ServerConnection) -> None:
        for message in ws:
            text = message if isinstance(message, str) else message.decode()
            received.append(text)
            request = json.loads(text)
            if request.get("op") == "subscribe":
                ws.send(json.dumps({"success": True, "op": "subscribe", "conn_id": "c"}))
                ws.send(json.dumps(FRAME))
                second = {**FRAME, "data": [{**ROW, "T": 1672304486900, "i": "second"}]}
                ws.send(json.dumps(second))
                ws.send(json.dumps({"topic": "orderbook.50.BTCUSDT", "data": {}}))
                return

    with serve(handler, "127.0.0.1", 0) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.socket.getsockname()[1]
        try:
            yield f"ws://127.0.0.1:{port}", received
        finally:
            srv.shutdown()


def test_feed_subscribes_reads_trades_and_counts_what_it_does_not_know(
    server: tuple[str, list[str]],
) -> None:
    url, received = server
    stop = threading.Event()
    feed = BybitPublicFeed("BTCUSDT", url=url, max_reconnects=0, stop=stop)
    out = []
    for trade in feed:
        out.append(trade)
        if len(out) == 2:
            stop.set()
    assert [t.trade_id for t in out] == ["20f43950-d8dd-5b31-9112-a178eb6023af", "second"]
    assert json.loads(received[0]) == {"op": "subscribe", "args": ["publicTrade.BTCUSDT"]}
    assert feed.health.connects == 1 and feed.health.trades == 2


def test_an_unacknowledged_subscribe_is_not_a_subscription() -> None:
    def silent(ws: ServerConnection) -> None:
        for _ in ws:
            pass

    with serve(silent, "127.0.0.1", 0) as srv:
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        port = srv.socket.getsockname()[1]
        import trading_research_agents.trading.feeds.bybit as module

        original = module.SUBSCRIBE_ACK_TIMEOUT_S
        module.SUBSCRIBE_ACK_TIMEOUT_S = 0.5
        try:
            feed = BybitPublicFeed("BTCUSDT", url=f"ws://127.0.0.1:{port}", max_reconnects=0)
            with pytest.raises(FeedError, match="gave up"):
                list(feed)
            assert feed.health.unrecognised["disconnect: FeedError"] == 1
        finally:
            module.SUBSCRIBE_ACK_TIMEOUT_S = original
            srv.shutdown()


def test_the_feed_is_a_registered_plugin() -> None:
    assert "bybit" in live_feeds()
    feed = live_feed("BYBIT")("BTCUSDT", stop=threading.Event(), max_reconnects=0)
    assert isinstance(feed, BybitPublicFeed)
    with pytest.raises(UnknownFeedError, match="registered"):
        live_feed("no-such-feed")
