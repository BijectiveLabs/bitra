"""Venue archive rows to canonical trades.

Each normaliser checks the layout it was written against on every file, not once at
integration time. A venue that changes a column silently should stop the pipeline here, with
the header it actually sent, rather than produce a dataset that looks right.

Layouts were read from the venues' own archives on 2026-09-12:

Bybit, https://public.bybit.com/trading/<SYMBOL>/<SYMBOL><YYYY-MM-DD>.csv.gz, with header::

    timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional
    1735689600.0974,BTCUSDT,Sell,0.003,93530.00,ZeroMinusTick,e807b277-...,2.8059e+10,0.003,280.59

Binance spot, https://data.binance.vision/data/spot/monthly/trades/<SYMBOL>/<SYMBOL>-trades-<YYYY-MM>.zip,
one CSV member, no header, columns trade_id, price, qty, quote_qty, time, is_buyer_maker,
is_best_match::

    4359935386,93576.00000000,0.00136000,127.26336000,1735689600010866,True,True

Binance time is milliseconds in files before 2025 and microseconds from 2025-01 on. The unit
is inferred from the digit count and reported, never assumed.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from decimal import Decimal, InvalidOperation

from trading_research_agent.data.schema import Rejection, Side, Trade

NORMALISER_VERSION = "2026.09.12"

BYBIT_VENUE = "bybit"
BYBIT_HEADER = (
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
BYBIT_SIDES = {"Buy": Side.BUY, "Sell": Side.SELL}

BINANCE_VENUE = "binance"
BINANCE_COLUMNS = 7
NS_PER_UNIT = {13: 1_000_000, 16: 1_000, 19: 1}
UNIT_NAME = {13: "ms", 16: "us", 19: "ns"}


class ArchiveFormatError(ValueError):
    """The file is not laid out the way this normaliser was written against."""


def bybit_trades(lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
    reader = csv.reader(lines)
    try:
        header = tuple(h.strip() for h in next(reader))
    except StopIteration:
        raise ArchiveFormatError("bybit archive is empty; expected a header row") from None
    if header != BYBIT_HEADER:
        raise ArchiveFormatError(f"bybit header changed: got {header}")

    for number, row in enumerate(reader, start=2):
        raw = ",".join(row)
        if len(row) != len(BYBIT_HEADER):
            yield Rejection(number, f"expected {len(BYBIT_HEADER)} columns, got {len(row)}", raw)
            continue
        ts_text, symbol, side_text, size, price, _tick, match_id = row[:7]
        if symbol != instrument:
            yield Rejection(number, f"row is for {symbol!r}, file is for {instrument!r}", raw)
            continue
        side = BYBIT_SIDES.get(side_text)
        if side is None:
            yield Rejection(number, f"unknown side {side_text!r}", raw)
            continue
        try:
            ts_ns = int(Decimal(ts_text) * 1_000_000_000)
            trade = Trade(
                ts_ns=ts_ns,
                venue=BYBIT_VENUE,
                instrument=instrument,
                price=Decimal(price),
                qty=Decimal(size),
                side=side,
                trade_id=match_id,
            )
        except (InvalidOperation, ValueError) as exc:
            yield Rejection(number, str(exc), raw)
            continue
        yield trade


def binance_time_unit(digits: int) -> str | None:
    return UNIT_NAME.get(digits)


def binance_trades(lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
    reader = csv.reader(lines)
    for number, row in enumerate(reader, start=1):
        raw = ",".join(row)
        if len(row) != BINANCE_COLUMNS:
            yield Rejection(number, f"expected {BINANCE_COLUMNS} columns, got {len(row)}", raw)
            continue
        trade_id, price, qty, _quote, time_text, buyer_maker, _best = row
        if not time_text.isdigit() or len(time_text) not in NS_PER_UNIT:
            yield Rejection(number, f"time {time_text!r} is not ms, us or ns since epoch", raw)
            continue
        if buyer_maker not in ("True", "False", "true", "false"):
            yield Rejection(number, f"is_buyer_maker {buyer_maker!r} is not a boolean", raw)
            continue
        # If the buyer was the maker, the seller crossed the spread: the taker sold.
        side = Side.SELL if buyer_maker in ("True", "true") else Side.BUY
        try:
            trade = Trade(
                ts_ns=int(time_text) * NS_PER_UNIT[len(time_text)],
                venue=BINANCE_VENUE,
                instrument=instrument,
                price=Decimal(price),
                qty=Decimal(qty),
                side=side,
                trade_id=trade_id,
                seq=int(trade_id),
            )
        except (InvalidOperation, ValueError) as exc:
            yield Rejection(number, str(exc), raw)
            continue
        yield trade
