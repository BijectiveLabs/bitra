"""One hundred venues through one interface, for public market data only.

Verified against ccxt 4.5.84 on 2026-09-27: `fetch_ohlcv` returns rows of
`[timestamp_ms, open, high, low, close, volume]`; `fetch_order_book` returns `bids` and `asks`
as `[price, amount]` pairs with a millisecond `timestamp`; `fetch_funding_rate` returns
`fundingRate` with `timestamp` and `fundingTimestamp`; `fetch_open_interest` returns
`openInterestAmount` and `openInterestValue`. Errors arrive as `RateLimitExceeded`,
`NetworkError`, `BadSymbol` and other subclasses of `BaseError`, and all of them become a
`SourceError` so that a chain moves on instead of a session ending.

Two deliberate limits. This source reads public endpoints and is never given a key: data and
execution are different jobs, and nothing here should be able to place an order. And it hands
back only closed candles, because the candle a venue is still building is a price that has not
finished happening.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from trading_research_agents.data.schema import Candle
from trading_research_agents.data.sources.base import (
    NS_PER_S,
    BookLevel,
    Fetched,
    OrderBook,
    SourceError,
)

TIMEFRAMES = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1800: "30m",
    3600: "1h",
    7200: "2h",
    14400: "4h",
    21600: "6h",
    43200: "12h",
    86400: "1d",
}
DEFAULT_INTERVAL_S = 60
DEFAULT_LIMIT = 240
DEFAULT_BOOK_LEVELS = 25


def _decimal(value: Any, label: str) -> Decimal:
    """A vendor's JSON number to a Decimal through its own decimal string, never through binary."""
    if value is None:
        raise SourceError(f"{label} is missing")
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise SourceError(f"{label} is not a number: {value!r}") from exc


class CcxtSource:
    """Public data from one venue through ccxt. The venue name is the ccxt exchange id."""

    def __init__(
        self,
        venue: str,
        *,
        client: Any = None,
        tier: str = "free",
        symbols: dict[str, str] | None = None,
        clock: Any = time.time,
        settings: dict[str, Any] | None = None,
    ) -> None:
        if not venue.strip():
            raise ValueError("a ccxt source names its venue")
        self.venue = venue.strip()
        self.symbols = dict(symbols or {})
        self.clock = clock
        self._tier = tier
        if client is None:
            client = self._build(settings or {})
        self._client = client

    def _build(self, settings: dict[str, Any]) -> Any:
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise SourceError(
                "ccxt is not installed; pip install 'trading-research-agents[ccxt]'"
            ) from exc
        factory = getattr(ccxt, self.venue, None)
        if factory is None:
            raise SourceError(f"ccxt has no exchange named {self.venue!r}")
        # Public data only: no key, no secret, and the library's own rate limiter on.
        return factory({"enableRateLimit": True, **settings})

    @property
    def name(self) -> str:
        return f"ccxt:{self.venue}"

    @property
    def tier(self) -> str:
        return self._tier

    def kinds(self) -> tuple[str, ...]:
        return ("ohlcv", "orderbook", "funding", "open_interest")

    def fetch(self, kind: str, instrument: str, **options: Any) -> Fetched:
        symbol = self._symbol(instrument)
        try:
            if kind == "ohlcv":
                return self._ohlcv(instrument, symbol, **options)
            if kind == "orderbook":
                return self._orderbook(instrument, symbol, **options)
            if kind == "funding":
                return self._funding(instrument, symbol)
            if kind == "open_interest":
                return self._open_interest(instrument, symbol)
        except SourceError:
            raise
        except Exception as exc:
            # Every vendor failure is a failed source, named by its own class so the attempt
            # line in the snapshot says whether it was a rate limit, a network drop or a symbol.
            raise SourceError(f"{exc.__class__.__name__}: {exc}") from exc
        raise SourceError(f"{self.name} does not serve {kind}")

    def _symbol(self, instrument: str) -> str:
        """The venue's own instrument name to a ccxt unified symbol, declared or looked up."""
        if instrument in self.symbols:
            return self.symbols[instrument]
        if "/" in instrument:
            return instrument
        markets_by_id = getattr(self._client, "markets_by_id", None)
        if not markets_by_id:
            try:
                self._client.load_markets()
            except Exception as exc:
                raise SourceError(f"{self.name}: cannot load markets: {exc}") from exc
            markets_by_id = getattr(self._client, "markets_by_id", None) or {}
        found = markets_by_id.get(instrument) or []
        if isinstance(found, dict):
            found = [found]
        # A venue id can map to several markets (spot and swap). Perpetuals first: this package
        # is about perpetual futures, and an operator who wants spot declares the symbol.
        for market in sorted(found, key=lambda m: 0 if m.get("swap") else 1):
            symbol = market.get("symbol")
            if symbol:
                return str(symbol)
        raise SourceError(f"{self.name}: no market for {instrument!r}; declare its symbol")

    def _timeframe(self, interval_s: int) -> str:
        timeframe = TIMEFRAMES.get(interval_s)
        if timeframe is None:
            raise SourceError(
                f"no ccxt timeframe for {interval_s}s; use one of {sorted(TIMEFRAMES)}"
            )
        supported = getattr(self._client, "timeframes", None)
        if supported and timeframe not in supported:
            raise SourceError(f"{self.name} does not publish a {timeframe} timeframe")
        return timeframe

    def _ohlcv(self, instrument: str, symbol: str, **options: Any) -> Fetched:
        interval_s = int(options.get("interval_s", DEFAULT_INTERVAL_S))
        limit = int(options.get("limit", DEFAULT_LIMIT))
        if limit <= 0:
            raise SourceError("limit is positive")
        timeframe = self._timeframe(interval_s)
        # One extra row: the last one is usually still forming and is dropped below.
        rows = self._client.fetch_ohlcv(symbol, timeframe, None, limit + 1)
        if not rows:
            raise SourceError(f"{self.name}: no candles for {symbol}")
        now = self.clock()
        interval_ms = interval_s * 1000
        candles: list[Candle] = []
        for row in rows:
            start_ms = int(row[0])
            if (start_ms + interval_ms) / 1000 > now:
                continue
            candles.append(
                Candle(
                    start_ns=start_ms * 1_000_000,
                    open=_decimal(row[1], "open"),
                    high=_decimal(row[2], "high"),
                    low=_decimal(row[3], "low"),
                    close=_decimal(row[4], "close"),
                    volume=_decimal(row[5], "volume"),
                )
            )
        if not candles:
            raise SourceError(f"{self.name}: no closed candle for {symbol} yet")
        window = candles[-limit:]
        return Fetched(
            kind="ohlcv",
            instrument=instrument,
            value=window,
            source=self.name,
            tier=self.tier,
            fetched_at=now,
            as_of_ns=window[-1].start_ns + interval_s * NS_PER_S,
            detail={
                "candles": len(window),
                "interval_s": interval_s,
                "last_close": str(window[-1].close),
                "symbol": symbol,
            },
        )

    def _orderbook(self, instrument: str, symbol: str, **options: Any) -> Fetched:
        levels = int(options.get("levels", DEFAULT_BOOK_LEVELS))
        if levels <= 0:
            raise SourceError("levels is positive")
        raw = self._client.fetch_order_book(symbol, levels)
        bids = tuple(
            BookLevel(_decimal(p, "bid price"), _decimal(q, "bid size"))
            for p, q in list(raw.get("bids") or [])[:levels]
        )
        asks = tuple(
            BookLevel(_decimal(p, "ask price"), _decimal(q, "ask size"))
            for p, q in list(raw.get("asks") or [])[:levels]
        )
        if not bids or not asks:
            raise SourceError(f"{self.name}: {symbol} book came back one-sided")
        book = OrderBook(bids=bids, asks=asks)
        now = self.clock()
        stamp = raw.get("timestamp")
        as_of_ns = int(stamp) * 1_000_000 if stamp else int(now * NS_PER_S)
        return Fetched(
            kind="orderbook",
            instrument=instrument,
            value=book,
            source=self.name,
            tier=self.tier,
            fetched_at=now,
            as_of_ns=as_of_ns,
            detail={"symbol": symbol, **book.record()},
        )

    def _funding(self, instrument: str, symbol: str) -> Fetched:
        raw = self._client.fetch_funding_rate(symbol)
        rate = _decimal(raw.get("fundingRate"), "fundingRate")
        now = self.clock()
        stamp = raw.get("timestamp") or raw.get("fundingTimestamp")
        as_of_ns = int(stamp) * 1_000_000 if stamp else int(now * NS_PER_S)
        detail: dict[str, Any] = {"symbol": symbol, "rate": str(rate)}
        for key in ("interval", "nextFundingTimestamp", "markPrice"):
            if raw.get(key) is not None:
                detail[key] = str(raw[key])
        return Fetched(
            kind="funding",
            instrument=instrument,
            value=rate,
            source=self.name,
            tier=self.tier,
            fetched_at=now,
            as_of_ns=as_of_ns,
            detail=detail,
        )

    def _open_interest(self, instrument: str, symbol: str) -> Fetched:
        raw = self._client.fetch_open_interest(symbol)
        amount = raw.get("openInterestAmount")
        value = raw.get("openInterestValue")
        if amount is None and value is None:
            raise SourceError(f"{self.name}: {symbol} reports no open interest")
        now = self.clock()
        stamp = raw.get("timestamp")
        as_of_ns = int(stamp) * 1_000_000 if stamp else int(now * NS_PER_S)
        measured = _decimal(amount if amount is not None else value, "openInterest")
        return Fetched(
            kind="open_interest",
            instrument=instrument,
            value=measured,
            source=self.name,
            tier=self.tier,
            fetched_at=now,
            as_of_ns=as_of_ns,
            detail={
                "symbol": symbol,
                "amount": None if amount is None else str(amount),
                "value": None if value is None else str(value),
            },
        )
