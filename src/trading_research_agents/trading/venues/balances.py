"""A balance ccxt fetched but did not parse, read from the venue's own answer.

Binance futures on the demo API (2026-10-20, ccxt 4.5.85): `GET /fapi/v3/account` answered with
5000 USDT in `assets`, and ccxt's unified balance came back with nothing in it, so every order
would have been refused for want of a balance. The venue's own rows are read instead, with the
same meaning ccxt gives them: the wallet as the total, what is available as free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


def _binance(balance: Mapping[str, Any], currency: str) -> dict[str, Any] | None:
    info = balance.get("info")
    rows = info.get("assets") if isinstance(info, Mapping) else None
    for row in rows or []:
        if isinstance(row, Mapping) and row.get("asset") == currency:
            return {"total": row.get("walletBalance"), "free": row.get("availableBalance")}
    return None


READS: dict[str, Callable[[Mapping[str, Any], str], dict[str, Any] | None]] = {
    "binance": _binance,
    "binanceusdm": _binance,
    "binancecoinm": _binance,
}


def unparsed(venue: str, balance: Mapping[str, Any], currency: str) -> dict[str, Any] | None:
    """The currency's row from the venue's own answer, when ccxt's parse left it out."""
    read = READS.get(venue.strip().lower())
    return read(balance, currency) if read is not None else None
