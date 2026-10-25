"""Bybit public trade archives: one gzip CSV per instrument per UTC day.

Layout read from the venue's own archive on 2026-09-12::

    timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional
    1735689600.0974,BTCUSDT,Sell,0.003,93530.00,ZeroMinusTick,e807b277-...,2.8059e+10,0.003,280.59

The venue later republished its files with an eleventh column, `RPI`, which marks a trade
against a Retail Price Improvement order (read from the file for 2025-07-01 on 2026-10-25). It is
still a trade on the book, so both layouts are read and normalise the same way.

The header is checked on every file. A venue that changes a column stops the pipeline here,
with the header it actually sent, rather than producing a dataset that looks right.

Funding comes from the public v5 endpoint `GET /v5/market/funding/history` (category, symbol,
startTime, endTime, limit 1..200; `fundingRate` and `fundingRateTimestamp` in milliseconds as
strings), read from the venue's documentation on 2026-09-13. Pages walk backwards from
`endTime`, newest first, which is how the venue orders them.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from trading_research_agents.data.fetch import FetchError, fetch_json
from trading_research_agents.data.normalise import ArchiveFormatError
from trading_research_agents.data.schema import Rejection, Side, Trade
from trading_research_agents.data.venues.registry import FundingRate, register

BASE = "https://public.bybit.com/trading"
API = "https://api.bybit.com"
FUNDING_PAGE = 200
NS_PER_MS = 1_000_000
HEADER = (
    "timestamp",
    "symbol",
    "side",
    "size",
    "price",
    "tickDirection",
    "trdMatchID",
    "grossValue",
    "homeNotional",
    "foreignNotional",
)
LAYOUTS = (HEADER, (*HEADER, "RPI"))
SIDES = {"Buy": Side.BUY, "Sell": Side.SELL}


class Bybit:
    name = "bybit"
    cadence = "daily"

    def __init__(self, *, api_base: str = API) -> None:
        self.api_base = api_base.rstrip("/")

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if day is None:
            raise ValueError("bybit archives are daily; a day is required")
        return f"{BASE}/{instrument}/{instrument}{day.isoformat()}.csv.gz"

    def checksum_url(self, archive_url: str) -> str | None:
        return None

    def funding_rates(self, instrument: str, *, start_ms: int, end_ms: int) -> list[FundingRate]:
        """Every settlement in [start_ms, end_ms], oldest first. A page that lies is refused."""
        if end_ms < start_ms:
            raise ValueError("end is before start")
        rows: dict[int, Decimal] = {}
        cursor = end_ms
        while cursor >= start_ms:
            query = urlencode(
                {
                    "category": "linear",
                    "symbol": instrument,
                    "startTime": str(start_ms),
                    "endTime": str(cursor),
                    "limit": str(FUNDING_PAGE),
                }
            )
            payload = fetch_json(f"{self.api_base}/v5/market/funding/history?{query}")
            if not isinstance(payload, dict) or payload.get("retCode") != 0:
                raise FetchError(f"funding history refused: {payload!r}"[:300])
            page = (payload.get("result") or {}).get("list") or []
            if not page:
                break
            oldest = cursor
            for row in page:
                try:
                    ts_ms = int(row["fundingRateTimestamp"])
                    rate = Decimal(str(row["fundingRate"]))
                except (KeyError, ValueError, InvalidOperation) as exc:
                    raise FetchError(f"funding row is not a settlement: {row!r}") from exc
                if row.get("symbol") not in (None, instrument):
                    raise FetchError(
                        f"funding row is for {row.get('symbol')!r}, not {instrument!r}"
                    )
                if start_ms <= ts_ms <= end_ms:
                    rows[ts_ms * NS_PER_MS] = rate
                oldest = min(oldest, ts_ms)
            if len(page) < FUNDING_PAGE or oldest <= start_ms:
                break
            cursor = oldest - 1
        return [FundingRate(ts, rows[ts]) for ts in sorted(rows)]

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        reader = csv.reader(lines)
        try:
            header = tuple(h.strip() for h in next(reader))
        except StopIteration:
            raise ArchiveFormatError("bybit archive is empty; expected a header row") from None
        if header not in LAYOUTS:
            raise ArchiveFormatError(f"bybit header changed: got {header}")
        for number, row in enumerate(reader, start=2):
            raw = ",".join(row)
            if len(row) != len(header):
                yield Rejection(number, f"expected {len(header)} columns, got {len(row)}", raw)
                continue
            ts_text, symbol, side_text, size, price, _tick, match_id = row[:7]
            if symbol != instrument:
                yield Rejection(number, f"row is for {symbol!r}, file is for {instrument!r}", raw)
                continue
            side = SIDES.get(side_text)
            if side is None:
                yield Rejection(number, f"unknown side {side_text!r}", raw)
                continue
            try:
                yield Trade(
                    ts_ns=int(Decimal(ts_text) * 1_000_000_000),
                    venue=self.name,
                    instrument=instrument,
                    price=Decimal(price),
                    qty=Decimal(size),
                    side=side,
                    trade_id=match_id,
                )
            except (InvalidOperation, ValueError) as exc:
                yield Rejection(number, str(exc), raw)


register(Bybit())
