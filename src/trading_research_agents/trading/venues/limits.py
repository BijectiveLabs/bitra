"""The price band a venue accepts orders in, where it publishes one and ccxt does not unify it.

OKX refuses an order priced outside its current limits (sCode 51006, "Order price is not within the
price limit") and publishes them per instrument at `GET /api/v5/public/price-limit`, ccxt's
`publicGetPublicPriceLimit`. Seen on the OKX demo on 2026-10-17: a stock perpetual's weekend book
sat at the edge of its band, so a taker entry priced a few bps through the ask was refused whole.
A venue with no entry here has no band this package knows of, and none is applied.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any


def _okx(client: Any, symbol: str) -> tuple[Decimal | None, Decimal | None]:
    read = getattr(client, "publicGetPublicPriceLimit", None)
    if read is None:
        return None, None
    market = client.market(symbol)
    row = ((read({"instId": market["id"]}) or {}).get("data") or [{}])[0]
    if str(row.get("enabled", True)).lower() == "false":
        return None, None
    try:
        high = Decimal(str(row["buyLmt"])) if row.get("buyLmt") else None
        low = Decimal(str(row["sellLmt"])) if row.get("sellLmt") else None
    except (InvalidOperation, ValueError):
        return None, None
    return high, low


BANDS: dict[str, Callable[[Any, str], tuple[Decimal | None, Decimal | None]]] = {
    "okx": _okx,
    "myokx": _okx,
    "okxus": _okx,
}


def price_band(venue: str, client: Any, symbol: str) -> tuple[Decimal | None, Decimal | None]:
    """The highest price a buy and the lowest a sell may carry right now; None where unknown."""
    read = BANDS.get(venue.strip().lower())
    if read is None:
        return None, None
    try:
        return read(client, symbol)
    except Exception:
        # Not knowing the band is not a reason to refuse; the venue still refuses at its end.
        return None, None
