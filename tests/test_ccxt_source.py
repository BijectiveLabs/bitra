"""The ccxt adapter against the vendor contract its docstring names.

The live check is in the Labs evidence; this pins the parts that are ours: the still-forming
candle is dropped, a vendor number goes through its own decimal string, a venue's own parameters
are handed over verbatim, and every vendor failure comes back as one kind of error so a chain can
move on. The double answers the way ccxt 4.5.84 answers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.data.sources import SourceError
from trading_research_agents.data.sources.base import OrderBook
from trading_research_agents.data.sources.ccxt_source import CcxtSource

MINUTE_MS = 60_000
OPEN_AT = 1_790_000_000_000  # a round millisecond, so the arithmetic in the test reads


class FakeCcxt:
    """What ccxt hands back, in the shapes the adapter's docstring records."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.seen: list[tuple[str, tuple[Any, ...]]] = []
        self.markets_by_id: dict[str, Any] = answers.get("markets_by_id", {})
        self.loaded = 0

    def load_markets(self) -> None:
        self.loaded += 1
        self.markets_by_id = self.answers.get("on_load", {})

    def _answer(self, name: str, args: tuple[Any, ...]) -> Any:
        self.seen.append((name, args))
        answer = self.answers.get(name)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fetch_ohlcv(self, *args: Any) -> Any:
        return self._answer("fetch_ohlcv", args)

    def fetch_order_book(self, *args: Any) -> Any:
        return self._answer("fetch_order_book", args)

    def fetch_funding_rate(self, *args: Any) -> Any:
        return self._answer("fetch_funding_rate", args)

    def fetch_open_interest(self, *args: Any) -> Any:
        return self._answer("fetch_open_interest", args)


def rows(count: int) -> list[list[Any]]:
    out = []
    for i in range(count):
        base = 100 + i
        out.append([OPEN_AT + i * MINUTE_MS, base, base + 2, base - 1, base + 1, 10 + i])
    return out


def source(client: FakeCcxt, *, now_ms: int, **kwargs: Any) -> CcxtSource:
    return CcxtSource(
        "bybit",
        client=client,
        symbols={"BTCUSDT": "BTC/USDT:USDT"},
        clock=lambda: now_ms / 1000,
        **kwargs,
    )


def test_the_candle_the_venue_is_still_building_is_dropped() -> None:
    client = FakeCcxt(fetch_ohlcv=rows(4))
    # Two minutes into the fourth candle's minute: three have closed.
    src = source(client, now_ms=OPEN_AT + 3 * MINUTE_MS + 30_000)

    got = src.fetch("ohlcv", "BTCUSDT", interval_s=60, limit=3)
    candles = got.value
    assert len(candles) == 3
    assert candles[-1].close == Decimal("103")
    assert got.as_of_ns == (OPEN_AT + 3 * MINUTE_MS) * 1_000_000
    assert got.detail["interval_s"] == 60 and got.detail["symbol"] == "BTC/USDT:USDT"
    # One extra row is asked for, because the last one is usually the one still forming.
    assert client.seen[0][1][:4] == ("BTC/USDT:USDT", "1m", None, 4)


def test_only_closed_candles_means_a_venue_can_have_none_yet() -> None:
    client = FakeCcxt(fetch_ohlcv=rows(1))
    src = source(client, now_ms=OPEN_AT + 10_000)

    with pytest.raises(SourceError, match="no closed candle"):
        src.fetch("ohlcv", "BTCUSDT", interval_s=60, limit=3)


def test_an_interval_the_venue_does_not_publish_is_refused_before_the_call() -> None:
    client = FakeCcxt(fetch_ohlcv=rows(4))
    src = source(client, now_ms=OPEN_AT + 4 * MINUTE_MS)

    with pytest.raises(SourceError, match="no ccxt timeframe for 45s"):
        src.fetch("ohlcv", "BTCUSDT", interval_s=45)
    assert client.seen == []


def test_the_book_carries_its_own_stamp_and_refuses_one_sided() -> None:
    raw = {
        "bids": [[100.5, 2], [100.4, 3]],
        "asks": [[100.7, 1], [100.8, 4]],
        "timestamp": OPEN_AT,
    }
    src = source(FakeCcxt(fetch_order_book=raw), now_ms=OPEN_AT + 2_000)

    got = src.fetch("orderbook", "BTCUSDT", levels=2)
    book = got.value
    assert isinstance(book, OrderBook)
    assert book.mid == Decimal("100.6")
    assert got.as_of_ns == OPEN_AT * 1_000_000
    assert got.age_s((OPEN_AT + 2_000) / 1000) == pytest.approx(2.0)

    empty = source(FakeCcxt(fetch_order_book={"bids": [], "asks": [[1, 1]]}), now_ms=OPEN_AT)
    with pytest.raises(SourceError, match="one-sided"):
        empty.fetch("orderbook", "BTCUSDT")


def test_a_book_level_with_more_than_a_price_and_a_size_is_read() -> None:
    # OKX appends a deprecated field and the number of orders: [price, size, 0, orders].
    raw = {
        "bids": [[100.5, 2, 0, 3], [100.4, 3, 0, 1]],
        "asks": [[100.7, 1, 0, 2], [100.8, 4, 0, 5]],
        "timestamp": OPEN_AT,
    }
    got = source(FakeCcxt(fetch_order_book=raw), now_ms=OPEN_AT + 1_000).fetch(
        "orderbook", "BTCUSDT", levels=2
    )
    assert got.value.mid == Decimal("100.6")


def test_a_funding_rate_with_no_timestamp_is_as_old_as_it_was_read_not_the_next_settlement() -> (
    None
):
    now_ms = OPEN_AT + 1_000
    client = FakeCcxt(
        fetch_funding_rate={
            "fundingRate": "0.0001",
            "timestamp": None,
            # The next settlement, an hour from now: the venue's date, not the rate's.
            "fundingTimestamp": now_ms + 3_600_000,
        }
    )
    got = source(client, now_ms=now_ms).fetch("funding", "BTCUSDT")
    assert got.age_s(now_ms / 1000) == pytest.approx(0.0)


def test_funding_and_open_interest_keep_the_venues_own_decimal_string() -> None:
    client = FakeCcxt(
        fetch_funding_rate={"fundingRate": "0.00012345", "timestamp": OPEN_AT, "interval": "8h"},
        fetch_open_interest={"openInterestAmount": "55955.026", "timestamp": OPEN_AT},
    )
    src = source(client, now_ms=OPEN_AT + 1_000)

    funding = src.fetch("funding", "BTCUSDT")
    assert funding.value == Decimal("0.00012345") and funding.detail["interval"] == "8h"

    oi = src.fetch("open_interest", "BTCUSDT")
    assert oi.value == Decimal("55955.026")
    assert oi.as_of_ns == OPEN_AT * 1_000_000


def test_a_venues_own_parameters_are_handed_over_and_recorded() -> None:
    client = FakeCcxt(fetch_open_interest={"openInterestAmount": 1, "timestamp": OPEN_AT})
    src = source(client, now_ms=OPEN_AT)

    got = src.fetch("open_interest", "BTCUSDT", params={"intervalTime": "5min"})
    assert client.seen[0] == ("fetch_open_interest", ("BTC/USDT:USDT", {"intervalTime": "5min"}))
    assert got.detail["params"] == {"intervalTime": "5min"}


def test_a_figure_that_is_not_a_number_is_refused_by_name() -> None:
    client = FakeCcxt(fetch_funding_rate={"fundingRate": "n/a", "timestamp": OPEN_AT})
    src = source(client, now_ms=OPEN_AT)

    with pytest.raises(SourceError, match="fundingRate is not a number"):
        src.fetch("funding", "BTCUSDT")


def test_every_vendor_failure_is_one_kind_of_error_so_the_chain_moves_on() -> None:
    class RateLimitExceeded(Exception):  # noqa: N818 - ccxt's own spelling is the point
        pass

    client = FakeCcxt(fetch_ohlcv=RateLimitExceeded("too many requests"))
    src = source(client, now_ms=OPEN_AT + MINUTE_MS)

    with pytest.raises(SourceError, match="RateLimitExceeded: too many requests"):
        src.fetch("ohlcv", "BTCUSDT")


def test_a_venue_id_is_looked_up_and_the_perpetual_wins() -> None:
    client = FakeCcxt(
        on_load={
            "ETHUSDT": [
                {"symbol": "ETH/USDT", "swap": False},
                {"symbol": "ETH/USDT:USDT", "swap": True},
            ]
        },
        fetch_funding_rate={"fundingRate": "0.0001", "timestamp": OPEN_AT},
    )
    src = source(client, now_ms=OPEN_AT)

    src.fetch("funding", "ETHUSDT")
    assert client.loaded == 1
    assert client.seen[0][1][0] == "ETH/USDT:USDT"

    blank = source(FakeCcxt(), now_ms=OPEN_AT)
    with pytest.raises(SourceError, match="no linear perpetual named 'SOLUSDT'"):
        blank.fetch("funding", "SOLUSDT")


def test_the_adapter_says_what_it_does_not_serve() -> None:
    src = source(FakeCcxt(), now_ms=OPEN_AT)
    assert src.kinds() == ("ohlcv", "orderbook", "funding", "open_interest")
    assert src.name == "ccxt:bybit" and src.tier == "free"

    with pytest.raises(SourceError, match="does not serve sentiment"):
        src.fetch("sentiment", "BTCUSDT")
