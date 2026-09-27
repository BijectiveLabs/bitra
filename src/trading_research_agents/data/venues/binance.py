"""Binance spot public trade archives: one zip per instrument per month, one CSV inside.

Layout read from the venue's own archive on 2026-09-12: no header, columns trade_id, price,
qty, quote_qty, time, is_buyer_maker, is_best_match::

    4359935386,93576.00000000,0.00136000,127.26336000,1735689600010866,True,True

`time` is milliseconds in files before 2025 and microseconds from 2025-01 on. The unit is
inferred from the digit count and any other width is rejected. `is_buyer_maker = True` means
the buyer rested and the seller crossed, so the taker side is `sell`. The venue publishes a
`.CHECKSUM` sidecar per archive, which the fetch verifies.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from datetime import date
from decimal import Decimal, InvalidOperation

from trading_research_agents.data.schema import Rejection, Side, Trade
from trading_research_agents.data.venues.registry import register

BASE = "https://data.binance.vision/data/spot/monthly/trades"
COLUMNS = 7
NS_PER_UNIT = {13: 1_000_000, 16: 1_000, 19: 1}


class Binance:
    name = "binance"
    cadence = "monthly"

    def archive_url(
        self, instrument: str, *, day: date | None, month: tuple[int, int] | None
    ) -> str:
        if month is None:
            raise ValueError("binance archives are monthly; a month is required")
        year, mon = month
        return f"{BASE}/{instrument}/{instrument}-trades-{year:04d}-{mon:02d}.zip"

    def checksum_url(self, archive_url: str) -> str | None:
        return archive_url + ".CHECKSUM"

    def normalise(self, lines: Iterable[str], *, instrument: str) -> Iterator[Trade | Rejection]:
        for number, row in enumerate(csv.reader(lines), start=1):
            raw = ",".join(row)
            if len(row) != COLUMNS:
                yield Rejection(number, f"expected {COLUMNS} columns, got {len(row)}", raw)
                continue
            trade_id, price, qty, _quote, time_text, buyer_maker, _best = row
            if not time_text.isdigit() or len(time_text) not in NS_PER_UNIT:
                yield Rejection(number, f"time {time_text!r} is not ms, us or ns since epoch", raw)
                continue
            if buyer_maker not in ("True", "False", "true", "false"):
                yield Rejection(number, f"is_buyer_maker {buyer_maker!r} is not a boolean", raw)
                continue
            side = Side.SELL if buyer_maker in ("True", "true") else Side.BUY
            try:
                yield Trade(
                    ts_ns=int(time_text) * NS_PER_UNIT[len(time_text)],
                    venue=self.name,
                    instrument=instrument,
                    price=Decimal(price),
                    qty=Decimal(qty),
                    side=side,
                    trade_id=trade_id,
                    seq=int(trade_id),
                )
            except (InvalidOperation, ValueError) as exc:
                yield Rejection(number, str(exc), raw)


register(Binance())
