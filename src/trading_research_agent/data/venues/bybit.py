"""Bybit public trade archives: one gzip CSV per instrument per UTC day.

Layout read from the venue's own archive on 2026-09-12::

    timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional
    1735689600.0974,BTCUSDT,Sell,0.003,93530.00,ZeroMinusTick,e807b277-...,2.8059e+10,0.003,280.59

The header is checked on every file. A venue that changes a column stops the pipeline here,
with the header it actually sent, rather than producing a dataset that looks right.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation

from trading_research_agent.data.normalise import ArchiveFormatError
from trading_research_agent.data.schema import Rejection, Side, Trade
from trading_research_agent.data.venues.registry import register

BASE = "https://public.bybit.com/trading"
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
SIDES = {"Buy": Side.BUY, "Sell": Side.SELL}


class Bybit:
    name = "bybit"
    cadence = "daily"

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if day is None:
            raise ValueError("bybit archives are daily; a day is required")
        return f"{BASE}/{instrument}/{instrument}{day.isoformat()}.csv.gz"

    def checksum_url(self, archive_url: str) -> str | None:
        return None

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        reader = csv.reader(lines)
        try:
            header = tuple(h.strip() for h in next(reader))
        except StopIteration:
            raise ArchiveFormatError("bybit archive is empty; expected a header row") from None
        if header != HEADER:
            raise ArchiveFormatError(f"bybit header changed: got {header}")
        for number, row in enumerate(reader, start=2):
            raw = ",".join(row)
            if len(row) != len(HEADER):
                yield Rejection(number, f"expected {len(HEADER)} columns, got {len(row)}", raw)
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
