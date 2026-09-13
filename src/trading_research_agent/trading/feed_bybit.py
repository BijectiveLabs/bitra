"""The Bybit public trade stream, without a key, for shadow at layer 7.

Endpoint, subscribe frame, application-level ping and the trade message fields were taken
from the venue's own documentation (v5 WebSocket, public trade topic) and checked against the
platform's recorded frames: `publicTrade.<SYMBOL>` messages carry `data[]` items with `T`
(match time, milliseconds), `s`, `S` (the taker's side), `v`, `p` and `i` (trade id).

Three things this feed refuses to do quietly. A subscribe that is not acknowledged within the
deadline is not a subscription. A link carrying nothing for two ping intervals is dead even if
the socket looks open. A message it does not recognise is counted and shown, not dropped.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agent.data.schema import Side, Trade
from trading_research_agent.trading.feed import FeedError

PUBLIC_LINEAR = "wss://stream.bybit.com/v5/public/linear"
PING_INTERVAL_S = 20.0
SILENCE_LIMIT_S = 2 * PING_INTERVAL_S
SUBSCRIBE_ACK_TIMEOUT_S = 10.0
SIDES = {"Buy": Side.BUY, "Sell": Side.SELL}
VENUE = "bybit"


@dataclass
class FeedHealth:
    connects: int = 0
    reconnects: int = 0
    trades: int = 0
    unrecognised: Counter[str] = field(default_factory=Counter)
    rejected: int = 0
    last_message_at: float | None = None


def parse_public_trade(text: str, *, instrument: str) -> list[Trade] | None:
    """Trades from one frame, None when the frame is not a public trade message for us.

    A row that does not parse raises rather than being skipped: a partial batch would leave a
    gap that looks like a quiet market.
    """
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeedError(f"frame is not JSON: {exc}") from exc
    if not isinstance(message, dict) or message.get("topic") != f"publicTrade.{instrument}":
        return None
    rows = message.get("data")
    if not isinstance(rows, list):
        raise FeedError("publicTrade frame without a data list")
    out: list[Trade] = []
    for row in rows:
        try:
            side = SIDES[str(row["S"])]
            out.append(
                Trade(
                    ts_ns=int(row["T"]) * 1_000_000,
                    venue=VENUE,
                    instrument=str(row["s"]),
                    price=Decimal(str(row["p"])),
                    qty=Decimal(str(row["v"])),
                    side=side,
                    trade_id=str(row["i"]),
                )
            )
        except (KeyError, ValueError, InvalidOperation, TypeError) as exc:
            raise FeedError(f"unreadable trade row {row!r}: {exc}") from exc
    return out


class BybitPublicFeed:
    """Iterate live public trades for one instrument. Reconnects; never fabricates."""

    def __init__(
        self,
        instrument: str,
        *,
        url: str = PUBLIC_LINEAR,
        max_reconnects: int | None = None,
        stop: threading.Event | None = None,
        open_timeout_s: float = 10.0,
    ) -> None:
        if not instrument.strip():
            raise FeedError("a feed names its instrument")
        if not (
            url.startswith("wss://")
            or url.startswith("ws://localhost")
            or url.startswith("ws://127.0.0.1")
        ):
            raise FeedError("only wss, or ws on loopback for tests, is allowed")
        self._instrument = instrument
        self.url = url
        self.max_reconnects = max_reconnects
        self.stop = stop or threading.Event()
        self.open_timeout_s = open_timeout_s
        self.health = FeedHealth()
        self._early: list[str] = []

    @property
    def instrument(self) -> str:
        return self._instrument

    def __iter__(self) -> Iterator[Trade]:
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - exercised only where the extra is absent
            raise FeedError(
                "websockets is not installed; pip install 'trading-research-agent[shadow]'"
            ) from exc

        attempts = 0
        while not self.stop.is_set():
            if attempts > 0:
                if self.max_reconnects is not None and attempts > self.max_reconnects:
                    raise FeedError(f"gave up after {attempts - 1} reconnects")
                self.health.reconnects += 1
                time.sleep(min(2.0**attempts, 30.0))
            attempts += 1
            try:
                with connect(
                    self.url,
                    open_timeout=self.open_timeout_s,
                    ping_interval=None,
                    max_size=4 * 1024 * 1024,
                ) as ws:
                    self.health.connects += 1
                    ws.send(
                        json.dumps({"op": "subscribe", "args": [f"publicTrade.{self.instrument}"]})
                    )
                    if not self._await_ack(ws):
                        raise FeedError("subscribe not acknowledged; not subscribed")
                    last_ping = time.monotonic()
                    while not self.stop.is_set():
                        now = time.monotonic()
                        if now - last_ping >= PING_INTERVAL_S:
                            ws.send(json.dumps({"op": "ping"}))
                            last_ping = now
                        last = self.health.last_message_at
                        if last is not None and now - last > SILENCE_LIMIT_S:
                            raise FeedError("no message for two ping intervals; link is dead")
                        try:
                            text = ws.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        self.health.last_message_at = time.monotonic()
                        yield from self._handle(text if isinstance(text, str) else text.decode())
            except (ConnectionClosed, OSError, FeedError) as exc:
                if self.stop.is_set():
                    return
                self.health.unrecognised[f"disconnect: {type(exc).__name__}"] += 1
                continue
            return

    def _await_ack(self, ws: Any) -> bool:
        deadline = time.monotonic() + SUBSCRIBE_ACK_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                text = ws.recv(timeout=1.0)
            except TimeoutError:
                continue
            self.health.last_message_at = time.monotonic()
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                self.health.unrecognised["not json"] += 1
                continue
            if isinstance(message, dict) and message.get("op") == "subscribe":
                return bool(message.get("success"))
            # A trade can arrive before the acknowledgement; keep it rather than lose it.
            if isinstance(message, dict) and message.get("topic"):
                self._early.append(text if isinstance(text, str) else text.decode())
        return False

    def _handle(self, text: str) -> Iterator[Trade]:
        while self._early:
            yield from self._handle(self._early.pop(0))
        try:
            trades = parse_public_trade(text, instrument=self.instrument)
        except FeedError:
            self.health.rejected += 1
            raise
        if trades is None:
            try:
                message = json.loads(text)
                key = str(message.get("op") or message.get("topic") or "unknown")
            except (json.JSONDecodeError, AttributeError):
                key = "not json"
            if key not in ("ping", "pong"):
                self.health.unrecognised[key] += 1
            return
        self.health.trades += len(trades)
        yield from trades
