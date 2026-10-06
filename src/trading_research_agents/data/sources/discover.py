"""Every linear perpetual a venue lists, filtered to the ones a desk could actually trade.

Code does this part, every pass, from two public calls: the market list and one ticker for every
market. Nothing here ranks for opportunity; it only removes what cannot be traded at all (too thin,
too wide, too new, not crypto, not reachable at the desk's leverage) and orders the rest by how much
trades in it. Which of them is worth a cycle is the screener's question, asked later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

BPS = Decimal(10_000)
DAY_MS = 86_400_000
# ccxt's precision modes: in TICK_SIZE the amount precision is the step itself; in the other two
# it is a count of decimal places.
TICK_SIZE = 4

# Venues that list stocks, ETFs, commodities and currencies as perpetuals say so in their own market
# fields. Each entry names the field and the values that are not crypto. A venue missing here is
# filtered by [universe].exclude alone, and `tra universe` shows what came through.
NON_CRYPTO: dict[str, tuple[str, frozenset[str]]] = {
    "bybit": ("symbolType", frozenset({"stock", "etf", "commodity", "forex"})),
}


class DiscoverError(RuntimeError):
    """The venue's market list or tickers could not be read."""


@dataclass(frozen=True)
class Rules:
    """[universe] discovery settings."""

    quote: str = "USDT"
    top: int = 20
    min_quote_volume: Decimal = Decimal(10_000_000)
    max_spread_bps: Decimal = Decimal(5)
    min_listed_days: int = 30
    crypto_only: bool = True
    exclude: tuple[str, ...] = ()
    leverage: Decimal | None = None


@dataclass(frozen=True)
class Listed:
    """One tradeable perpetual: the venue's id for it, and the figures it passed on."""

    instrument: str
    symbol: str
    quote_volume: Decimal
    spread_bps: Decimal
    change_24h_pct: Decimal | None
    step: Decimal
    min_size: Decimal | None
    max_leverage: Decimal | None
    listed_days: int | None

    def record(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "symbol": self.symbol,
            "quote_volume_24h": str(self.quote_volume.quantize(Decimal(1))),
            "spread_bps": str(self.spread_bps),
            "change_24h_pct": None if self.change_24h_pct is None else str(self.change_24h_pct),
            "step": str(self.step),
            "min_size": None if self.min_size is None else str(self.min_size),
            "max_leverage": None if self.max_leverage is None else str(self.max_leverage),
            "listed_days": self.listed_days,
        }


@dataclass
class Found:
    """The shortlist, and how many markets each filter took out."""

    listed: list[Listed]
    dropped: dict[str, int]
    markets: int

    def record(self) -> dict[str, Any]:
        return {
            "markets": self.markets,
            "kept": [x.record() for x in self.listed],
            "dropped": dict(self.dropped),
        }


def discover(
    client: Any, venue: str, rules: Rules, *, clock: Callable[[], float] = time.time
) -> Found:
    """The venue's tradeable linear perpetuals, most traded first, at most `rules.top` of them."""
    try:
        markets = client.load_markets()
    except Exception as exc:
        raise DiscoverError(f"{venue}: cannot load markets: {exc.__class__.__name__}") from exc
    perps = {
        symbol: market
        for symbol, market in markets.items()
        if market.get("swap")
        and market.get("linear")
        and market.get("active", True)
        and str(market.get("settle") or "").upper() == rules.quote.upper()
    }
    try:
        tickers = client.fetch_tickers(list(perps))
    except Exception as exc:
        raise DiscoverError(f"{venue}: cannot read tickers: {exc.__class__.__name__}") from exc
    now_ms = clock() * 1000
    dropped: dict[str, int] = {}
    kept: list[Listed] = []
    tick = getattr(client, "precisionMode", TICK_SIZE) == TICK_SIZE
    excluded = {name.upper() for name in rules.exclude}
    for symbol, market in perps.items():
        instrument = str(market.get("id") or symbol)
        why = _unfit(instrument, market, tickers.get(symbol) or {}, venue, rules, now_ms, excluded)
        if isinstance(why, str):
            dropped[why] = dropped.get(why, 0) + 1
            continue
        kept.append(_listed(instrument, symbol, market, why, tick, now_ms))
    kept.sort(key=lambda x: x.quote_volume, reverse=True)
    if len(kept) > rules.top:
        dropped["beyond top"] = len(kept) - rules.top
    return Found(listed=kept[: rules.top], dropped=dropped, markets=len(perps))


def _unfit(
    instrument: str,
    market: Mapping[str, Any],
    ticker: Mapping[str, Any],
    venue: str,
    rules: Rules,
    now_ms: float,
    excluded: set[str],
) -> str | tuple[Decimal, Decimal, Decimal | None]:
    """Why a market cannot be traded, or the figures it passed on."""
    if instrument.upper() in excluded:
        return "excluded by the operator"
    if rules.crypto_only and venue in NON_CRYPTO:
        field_name, values = NON_CRYPTO[venue]
        kind = str((market.get("info") or {}).get(field_name) or "").lower()
        if kind in values:
            return "not crypto"
    volume = _dec(ticker.get("quoteVolume"))
    if volume is None or volume < rules.min_quote_volume:
        return "too little volume"
    bid, ask = _dec(ticker.get("bid")), _dec(ticker.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        return "no two-sided book"
    spread = ((ask - bid) / ((ask + bid) / 2) * BPS).quantize(Decimal("0.01"))
    if spread > rules.max_spread_bps:
        return "spread too wide"
    created = market.get("created")
    if created and (now_ms - float(created)) / DAY_MS < rules.min_listed_days:
        return "listed too recently"
    most = _dec(((market.get("limits") or {}).get("leverage") or {}).get("max"))
    if rules.leverage is not None and most is not None and most < rules.leverage:
        return "leverage above the venue's maximum"
    return volume, spread, _dec(ticker.get("percentage"))


def _listed(
    instrument: str,
    symbol: str,
    market: Mapping[str, Any],
    passed: tuple[Decimal, Decimal, Decimal | None],
    tick: bool,
    now_ms: float,
) -> Listed:
    precision = _dec((market.get("precision") or {}).get("amount"))
    if precision is None:
        step = Decimal(1)
    elif tick:
        step = precision
    else:
        step = Decimal(1).scaleb(-int(precision))
    created = market.get("created")
    leverage = _dec(((market.get("limits") or {}).get("leverage") or {}).get("max"))
    volume, spread, change = passed
    return Listed(
        instrument=instrument,
        symbol=symbol,
        quote_volume=volume,
        spread_bps=spread,
        change_24h_pct=change,
        step=step,
        min_size=_dec(((market.get("limits") or {}).get("amount") or {}).get("min")),
        max_leverage=leverage,
        listed_days=None if not created else int((now_ms - float(created)) // DAY_MS),
    )


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def merged(declared: Sequence[str], found: Found) -> tuple[str, ...]:
    """The operator's own list first, then what discovery added, each once."""
    return tuple(dict.fromkeys([*declared, *(x.instrument for x in found.listed)]))
