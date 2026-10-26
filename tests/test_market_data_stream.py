from __future__ import annotations

import asyncio
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from trading_research_agents.data.schema import Side, Trade
from trading_research_agents.data.series import Event
from trading_research_agents.market_data.book import BookSample
from trading_research_agents.market_data.deriv import DerivPoint
from trading_research_agents.market_data.record_config import RecordConfig
from trading_research_agents.market_data.venues.ccxt_stream import (
    Link,
    market_of,
    run_streams,
    to_liquidations,
)

NS = 1_000_000_000
START = 1_751_328_000 * NS


class Sink:
    def __init__(self) -> None:
        self.trades: list[Trade] = []
        self.samples: list[BookSample] = []
        self.points: list[DerivPoint] = []
        self.calls: list[tuple[str, str, str]] = []
        self.stopped = False

    def on_trades(self, instrument: str, trades: Iterable[Trade], received_ns: int) -> None:
        self.trades.extend(trades)

    def on_book(self, instrument: str, sample: BookSample) -> None:
        self.samples.append(sample)

    def on_points(self, instrument: str, points: Iterable[DerivPoint]) -> None:
        self.points.extend(points)

    def on_text(self, feed: str, events: Iterable[Event], now_ns: int) -> None:
        return None

    def on_disconnect(self, instrument: str, stream: str, at_ns: int, reason: str) -> None:
        self.calls.append(("disconnect", stream, reason))

    def on_reconnect(self, instrument: str, stream: str, at_ns: int) -> None:
        self.calls.append(("reconnect", stream, ""))

    def tick(self, now_ns: int) -> None:
        return None

    def stop(self, now_ns: int) -> None:
        self.stopped = True


class FakePro:
    """A ccxt.pro client for one contract market whose contract is a hundredth of a coin."""

    def __init__(self, *, liquidations: bool = True) -> None:
        self.markets = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "swap": True,
                "linear": True,
                "contract": True,
                "contractSize": 0.01,
                "precision": {"price": 0.1, "amount": 1},
            }
        }
        self.markets_by_id = {"BTCUSDT": [self.markets["BTC/USDT:USDT"]]}
        self.has = {"watchLiquidations": liquidations}
        self.batches: list[Any] = []
        self.books = 0
        self.liquidation_calls = 0
        self.clock: Clock | None = None

    async def watch_trades(self, symbol: str) -> Any:
        await asyncio.sleep(0)
        if not self.batches:
            return []
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def watch_order_book(self, symbol: str) -> Any:
        # The venue pushes a change every quarter of a second.
        await asyncio.sleep(0)
        if self.clock is not None:
            self.clock.now += NS // 4
        self.books += 1
        return {"bids": [[93530.1, 10]], "asks": [[93530.2, 5]]}

    async def watch_liquidations(self, symbol: str) -> Any:
        await asyncio.sleep(0)
        self.liquidation_calls += 1
        return []


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.slept: list[float] = []

    def __call__(self) -> int:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += int(seconds * NS)
        # While one task sleeps, the others run several times.
        for _ in range(4):
            await asyncio.sleep(0)


def run(client: FakePro, streams: list[str], seconds: int, sink: Sink) -> Clock:
    clock = Clock()
    client.clock = clock
    config = RecordConfig.from_table(
        {"venue": "bybit", "instruments": ["BTCUSDT"], "streams": streams}
    )
    link = Link(
        venue="bybit",
        client=client,
        recorder=sink,
        config=config,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0.0,
        stop_at_ns=START + seconds * NS,
    )
    asyncio.run(run_streams(link, [market_of(client, "BTCUSDT")]))
    return clock


def row(i: int) -> dict[str, Any]:
    return {
        "timestamp": 1_751_328_000_000 + i,
        "price": 93530.1,
        "amount": 3,
        "side": "buy",
        "id": f"t{i}",
    }


def test_streamed_trades_reach_the_recorder_in_the_base() -> None:
    client = FakePro()
    client.batches = [[row(1), row(2)]]
    sink = Sink()
    run(client, ["trades"], 3, sink)
    assert [t.qty for t in sink.trades] == [Decimal("0.03"), Decimal("0.03")]
    assert sink.trades[0].side is Side.BUY and sink.stopped
    assert market_of(client, "BTCUSDT").scales.price == 1


def test_a_dropped_stream_reconnects_with_backoff_and_is_marked() -> None:
    client = FakePro()
    client.batches = [ConnectionError("reset"), ConnectionError("reset"), [row(3)]]
    sink = Sink()
    clock = run(client, ["trades"], 10, sink)
    assert sink.calls == [
        ("disconnect", "trades", "ConnectionError: reset"),
        ("reconnect", "trades", ""),
    ]
    assert 1.0 in clock.slept and 2.0 in clock.slept and len(sink.trades) == 1


def test_liquidations_are_asked_only_where_the_venue_streams_them() -> None:
    without = FakePro(liquidations=False)
    run(without, ["liquidations"], 2, Sink())
    assert without.liquidation_calls == 0
    with_them = FakePro(liquidations=True)
    run(with_them, ["liquidations"], 2, Sink())
    assert with_them.liquidation_calls > 0


def test_the_book_is_sampled_not_recorded_whole() -> None:
    client = FakePro()
    sink = Sink()
    run(client, ["book"], 5, sink)
    assert client.books > len(sink.samples) > 0
    gaps = [b.at_ns - a.at_ns for a, b in zip(sink.samples, sink.samples[1:], strict=False)]
    assert all(gap >= NS for gap in gaps)
    assert sink.samples[0].bids[0] == (Decimal("93530.1"), Decimal("0.10"))


def test_a_bybit_liquidation_says_which_positions_were_liquidated() -> None:
    market = market_of(FakePro(), "BTCUSDT")
    rows = [{"timestamp": 1_751_328_000_000, "side": "buy", "contracts": 50, "price": 93000}]
    (point,) = to_liquidations(rows, venue="bybit", market=market, received_ns=START)
    assert point.get("liquidated") == "long" and point.value == Decimal("0.50")
    (other,) = to_liquidations(rows, venue="elsewhere", market=market, received_ns=START)
    assert other.get("liquidated") is None and other.get("side") == "buy"
