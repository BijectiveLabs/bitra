"""Every linear perpetual a venue lists, filtered to the ones a desk could actually trade.

Code does this part, every pass, from two public calls: the market list and one ticker for every
market. Nothing here ranks for opportunity; it only removes what cannot be traded at all (too thin,
too wide, too new, not crypto, not reachable at the desk's leverage) and orders the rest by how much
trades in it. Which of them is worth a cycle is the screener's question, asked later.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from trading_research_agents.data.sources.resolve import slug_of

BPS = Decimal(10_000)
DAY_MS = 86_400_000
# ccxt's precision modes: in TICK_SIZE the amount precision is the step itself; in the other two
# it is a count of decimal places.
TICK_SIZE = 4

# Venues that list stocks, ETFs, commodities and currencies as perpetuals say so in their own market
# fields. Each entry names the field and the values that are not crypto, and is only the default:
# [universe].type_field and exclude_types set or replace it for any venue.
NON_CRYPTO: dict[str, tuple[str, frozenset[str]]] = {
    "bybit": ("symbolType", frozenset({"stock", "etf", "commodity", "forex"})),
    # instCategory is 1 for crypto. 3 (stocks) and 4 (commodities) were read from the live market
    # list on 2026-10-08: 190 and 9 of 485 perpetuals. 5 and 6 are the venue's documented forex
    # and bond categories.
    "okx": ("instCategory", frozenset({"3", "4", "5", "6"})),
}


# What a market's underlying is, from the same fields: read from the venues' market lists on
# 2026-10-17. Bybit marks stock perpetuals `stock` and its tokenized-stock spot pairs `xstocks`;
# OKX's instCategory is a contract field and says nothing about a spot pair.
ASSET_CLASSES: dict[str, tuple[str, Mapping[str, str], bool]] = {
    "bybit": (
        "symbolType",
        {
            "stock": "equity",
            "etf": "equity",
            "xstocks": "equity",
            "commodity": "commodity",
            "forex": "forex",
        },
        False,
    ),
    "okx": ("instCategory", {"3": "equity", "4": "commodity", "5": "forex", "6": "bond"}, True),
}


def asset_class_of(venue: str, market: Mapping[str, Any]) -> str:
    """What the market's underlying is, as the venue says: equity, commodity, forex, bond, crypto.

    A venue that does not say is taken to list crypto, which is what this package was built on;
    an operator names the exceptions in [execution].equities.
    """
    entry = ASSET_CLASSES.get(venue.strip().lower())
    if entry is None:
        return "crypto"
    field, classes, contracts_only = entry
    if contracts_only and not market.get("contract"):
        return "crypto"
    value = str((market.get("info") or {}).get(field) or "").strip().lower()
    return classes.get(value, "crypto")


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
    # The venue's own field that says what a market is, and the values to leave out. Empty means
    # the venue's default from NON_CRYPTO, if it has one.
    type_field: str = ""
    exclude_types: tuple[str, ...] = ()

    def types_for(self, venue: str) -> tuple[str, frozenset[str]] | None:
        """The field and values that mark a market as not to be traded on this venue."""
        if self.type_field:
            return self.type_field, frozenset(t.lower() for t in self.exclude_types)
        return NON_CRYPTO.get(venue) if self.crypto_only else None


@dataclass(frozen=True)
class Listed:
    """One tradeable perpetual: the venue's id for it, and the figures it passed on."""

    instrument: str
    symbol: str
    quote_volume: Decimal
    spread_bps: Decimal
    change_24h_pct: Decimal | None
    # In base units. ccxt states both in contracts, and a contract is not always one unit of the
    # base: OKX BTC is 0.01, Gate BTC 0.0001, OKX PEPE 10 million.
    step: Decimal
    min_size: Decimal | None
    max_leverage: Decimal | None
    listed_days: int | None
    contract_size: Decimal = Decimal(1)

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
            "contract_size": str(self.contract_size),
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
        # Named by base and settlement currency, which every venue can resolve, not by the id the
        # venue happens to use: OKX says BTC-USDT-SWAP and Hyperliquid says 0.
        instrument = slug_of(market) or str(market.get("id") or symbol)
        if instrument in {x.instrument for x in kept}:
            dropped["same name as another market"] = (
                dropped.get("same name as another market", 0) + 1
            )
            continue
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
    marked = rules.types_for(venue)
    if marked is not None:
        field_name, values = marked
        kind = str((market.get("info") or {}).get(field_name) or "").lower()
        if kind in values:
            return "not crypto" if rules.crypto_only else f"{field_name} is {kind}"
    volume = _quote_volume(ticker, market)
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
    change = _dec(ticker.get("percentage"))
    return volume, spread, None if change is None else change.quantize(Decimal("0.0001"))


def market_step(market: Mapping[str, Any], *, tick: bool) -> Decimal:
    """The smallest size the venue takes for this market, in the base.

    ccxt states it in contracts, as a step or as a count of decimal places by venue, so it is
    converted through the contract size. Sizes are always in the base inside the desk.
    """
    precision = _dec((market.get("precision") or {}).get("amount"))
    contract = _dec(market.get("contractSize"))
    contract = contract if contract is not None and contract > 0 else Decimal(1)
    if precision is None:
        step = Decimal(1)
    elif tick:
        step = precision
    else:
        step = Decimal(1).scaleb(-int(precision))
    return step * contract


def _listed(
    instrument: str,
    symbol: str,
    market: Mapping[str, Any],
    passed: tuple[Decimal, Decimal, Decimal | None],
    tick: bool,
    now_ms: float,
) -> Listed:
    contract = _dec(market.get("contractSize"))
    contract = contract if contract is not None and contract > 0 else Decimal(1)
    least = _dec(((market.get("limits") or {}).get("amount") or {}).get("min"))
    created = market.get("created")
    leverage = _dec(((market.get("limits") or {}).get("leverage") or {}).get("max"))
    volume, spread, change = passed
    return Listed(
        instrument=instrument,
        symbol=symbol,
        quote_volume=volume,
        spread_bps=spread,
        change_24h_pct=change,
        step=market_step(market, tick=tick),
        min_size=None if least is None else least * contract,
        max_leverage=leverage,
        listed_days=None if not created else int((now_ms - float(created)) // DAY_MS),
        contract_size=contract,
    )


def _quote_volume(ticker: Mapping[str, Any], market: Mapping[str, Any]) -> Decimal | None:
    """What traded in 24 hours, in quote currency.

    Some venues leave quoteVolume empty on perpetuals and report baseVolume in contracts (seen on
    OKX, 2026-10-08: baseVolume 6,712,915 for Bitcoin, which is 67,129 BTC at 0.01 a contract). That
    is converted through the contract size and the last price rather than read as zero volume,
    which would have dropped every market on the venue.
    """
    direct = _dec(ticker.get("quoteVolume"))
    if direct is not None:
        return direct
    contracts, last = _dec(ticker.get("baseVolume")), _dec(ticker.get("last"))
    unit = _dec(market.get("contractSize"))
    if contracts is None or last is None or unit is None or unit <= 0:
        return None
    return contracts * unit * last


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def aligned(found: Found, client: Any, environment: str) -> Found:
    """The shortlist as the environment orders go to lists it.

    Ranking and filtering read the live market, where volume and spreads mean something. What an
    order is sized by, and whether the market exists at all, is read from the environment the
    order goes to, which on a demo or testnet API can differ market by market.
    """
    markets = getattr(client, "markets", None) or {}
    tick = getattr(client, "precisionMode", TICK_SIZE) == TICK_SIZE
    kept: list[Listed] = []
    missing = 0
    for row in found.listed:
        market = markets.get(row.symbol)
        if not market or not (market.get("swap") and market.get("linear")):
            missing += 1
            continue
        contract = _dec(market.get("contractSize"))
        contract = contract if contract is not None and contract > 0 else Decimal(1)
        least = _dec(((market.get("limits") or {}).get("amount") or {}).get("min"))
        kept.append(
            replace(
                row,
                step=market_step(market, tick=tick),
                min_size=None if least is None else least * contract,
                contract_size=contract,
            )
        )
    dropped = dict(found.dropped)
    if missing:
        dropped[f"not listed on {environment}"] = missing
    return Found(listed=kept, dropped=dropped, markets=found.markets)


def merged(declared: Sequence[str], found: Found) -> tuple[str, ...]:
    """The operator's own list first, then what discovery added, each once."""
    return tuple(dict.fromkeys([*declared, *(x.instrument for x in found.listed)]))
