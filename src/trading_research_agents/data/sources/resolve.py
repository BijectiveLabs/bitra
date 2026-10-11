"""Which market on a venue an instrument name means.

A name such as BTCUSDT is how some venues spell one perpetual. Others spell it BTC-USDT-SWAP,
BTC_USDT, XBTUSDTM or just 0, and on a venue that lists spot and perpetuals together the very same
string can also name the spot market. What every venue agrees on is ccxt's unified symbol,
BTC/USDT:USDT, and the pair of base and settlement currency behind it. So a name is resolved
against the venue that will be asked, in this order: a unified symbol as given, the venue's own
id, then base plus settle.
Only linear perpetuals are candidates; a name that is spot on one venue is never read as spot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


class ResolveError(ValueError):
    """The name matches no linear perpetual on the venue, or more than one."""


def slug_of(market: Mapping[str, Any]) -> str:
    """The venue-independent name of a perpetual: base and settlement currency, as BTCUSDT."""
    base, settle = market.get("base"), market.get("settle")
    return f"{base}{settle}".upper() if base and settle else ""


def linear_perpetual(market: Mapping[str, Any]) -> bool:
    return bool(market.get("swap") and market.get("linear") and market.get("active", True))


def resolve(
    name: str,
    markets_by_id: Mapping[str, Any],
    markets: Iterable[Mapping[str, Any]],
) -> str:
    """The ccxt unified symbol of the one linear perpetual `name` stands for on this venue."""
    if "/" in name:
        return name
    by_id = markets_by_id.get(name) or []
    if isinstance(by_id, Mapping):
        by_id = [by_id]
    found = [m for m in by_id if linear_perpetual(m) or _declared_swap(m)]
    if not found:
        wanted = name.upper()
        found = [m for m in markets if linear_perpetual(m) and slug_of(m) == wanted]
    symbols = sorted({str(m["symbol"]) for m in found if m.get("symbol")})
    if len(symbols) == 1:
        return symbols[0]
    if not symbols:
        raise ResolveError(f"no linear perpetual named {name!r} on this venue; declare its symbol")
    raise ResolveError(f"{name!r} is {', '.join(symbols)} on this venue; declare which")


def _declared_swap(market: Mapping[str, Any]) -> bool:
    # A market list that does not say `linear` (a thin test double, an older adapter) is still
    # a swap if it says so; what it must not be is spot.
    return bool(market.get("swap")) and "linear" not in market
