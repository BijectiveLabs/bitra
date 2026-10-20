"""A fill's fee as the venue charged it, where ccxt's reading of it is known to be wrong.

Bitget's unified account (v3 `GET /api/v3/trade/fills`, read on its demo API on 2026-10-20
through ccxt 4.5.85): a taker fill of 122.0625 USDT came back with `feeDetail` fee "0.0732375",
six basis points charged, and ccxt's unified `fee.cost` of -0.0732375, which reads as a rebate.
Counted that way, every round trip would show a better result than the account had. The venue's
own figure is read instead, positive for a fee charged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def _bitget(fill: Mapping[str, Any]) -> Decimal | None:
    info = fill.get("info") or {}
    detail = info.get("feeDetail") if isinstance(info, Mapping) else None
    # The v3 fill carries an execution id; the classic account's fills have another shape.
    if not isinstance(detail, list) or not detail or "execId" not in info:
        return None
    try:
        return sum((Decimal(str(row["fee"])) for row in detail), Decimal(0))
    except (KeyError, TypeError, InvalidOperation):
        return None


CHARGED: dict[str, Callable[[Mapping[str, Any]], Decimal | None]] = {"bitget": _bitget}


def charged(venue: str, fill: Mapping[str, Any]) -> Decimal | None:
    """The fee in the fill's fee currency, positive when charged, or None to keep ccxt's."""
    read = CHARGED.get(venue.strip().lower())
    return read(fill) if read is not None else None
