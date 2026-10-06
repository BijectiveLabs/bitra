"""Discovery: every perpetual the venue lists, cut down by code to what a desk could trade."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from trading_research_agents.data.sources.discover import DiscoverError, Rules, discover, merged

DAY_MS = 86_400_000
NOW_S = 1_800_000_000.0


def market(market_id: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": market_id,
        "swap": True,
        "linear": True,
        "active": True,
        "settle": "USDT",
        "created": NOW_S * 1000 - 400 * DAY_MS,
        "precision": {"amount": 0.01},
        "limits": {"amount": {"min": 0.01}, "leverage": {"max": 50}},
        "info": {"symbolType": ""},
    }
    base.update(extra)
    return base


class Venue:
    precisionMode = 4  # noqa: N815 - ccxt's own attribute name

    def __init__(self, markets: dict[str, Any], tickers: dict[str, Any]) -> None:
        self.markets, self.tickers = markets, tickers

    def load_markets(self) -> dict[str, Any]:
        return self.markets

    def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
        return {s: self.tickers[s] for s in symbols if s in self.tickers}


def ticker(volume: float, bid: float = 100.0, ask: float = 100.01, change: float = 1.5) -> Any:
    return {"quoteVolume": volume, "bid": bid, "ask": ask, "percentage": change}


def test_code_keeps_only_what_a_desk_could_trade_most_traded_first() -> None:
    markets = {
        "BTC/USDT:USDT": market("BTCUSDT"),
        "ZEC/USDT:USDT": market("ZECUSDT", precision={"amount": 0.1}),
        "SOXL/USDT:USDT": market("SOXLUSDT", info={"symbolType": "ETF"}),
        "THIN/USDT:USDT": market("THINUSDT"),
        "WIDE/USDT:USDT": market("WIDEUSDT"),
        "NEW/USDT:USDT": market("NEWUSDT", created=NOW_S * 1000 - 3 * DAY_MS),
        "LOW/USDT:USDT": market("LOWUSDT", limits={"leverage": {"max": 2}}),
        "BTC/USDT": market("BTCUSDT", swap=False),
        "BTC/USDC:USDC": market("BTCPERP", settle="USDC"),
        "SKIP/USDT:USDT": market("SKIPUSDT"),
    }
    tickers = {
        "BTC/USDT:USDT": ticker(4e9),
        "ZEC/USDT:USDT": ticker(4e8),
        "SOXL/USDT:USDT": ticker(9e8),
        "THIN/USDT:USDT": ticker(1e5),
        "WIDE/USDT:USDT": ticker(5e8, bid=100, ask=101),
        "NEW/USDT:USDT": ticker(5e8),
        "LOW/USDT:USDT": ticker(5e8),
        "SKIP/USDT:USDT": ticker(5e8),
    }
    found = discover(
        Venue(markets, tickers),
        "bybit",
        Rules(leverage=Decimal(3), exclude=("SKIPUSDT",)),
        clock=lambda: NOW_S,
    )
    assert [x.instrument for x in found.listed] == ["BTCUSDT", "ZECUSDT"]
    assert found.listed[1].step == Decimal("0.1") and found.listed[1].listed_days == 400
    assert found.dropped == {
        "not crypto": 1,
        "too little volume": 1,
        "spread too wide": 1,
        "listed too recently": 1,
        "leverage above the venue's maximum": 1,
        "excluded by the operator": 1,
    }
    assert found.markets == 8
    assert merged(("SOLUSDT", "BTCUSDT"), found) == ("SOLUSDT", "BTCUSDT", "ZECUSDT")


def test_the_shortlist_stops_at_top_and_decimal_place_venues_get_their_step() -> None:
    class Places(Venue):
        precisionMode = 2  # noqa: N815 - ccxt's own attribute name

    markets = {f"C{i}/USDT:USDT": market(f"C{i}USDT", precision={"amount": 3}) for i in range(5)}
    tickers = {f"C{i}/USDT:USDT": ticker(1e8 * (i + 1)) for i in range(5)}
    found = discover(Places(markets, tickers), "okx", Rules(top=2), clock=lambda: NOW_S)
    assert [x.instrument for x in found.listed] == ["C4USDT", "C3USDT"]
    assert found.listed[0].step == Decimal("0.001") and found.dropped["beyond top"] == 3


def test_a_venue_that_cannot_answer_is_named() -> None:
    class Down(Venue):
        def fetch_tickers(self, symbols: list[str]) -> dict[str, Any]:
            raise ConnectionError("timed out")

    with pytest.raises(DiscoverError, match="cannot read tickers"):
        discover(Down({"A/USDT:USDT": market("AUSDT")}, {}), "bybit", Rules())


def test_the_universe_declares_discovery_and_needs_a_quote_size() -> None:
    from test_config import agent_raw
    from trading_research_agents.trading.config import ConfigError, parse_strategy_config

    raw = agent_raw(universe={"discover": True, "top": 15, "exclude": ["pumpfunusdt"]})
    with pytest.raises(ConfigError, match=r"needs \[run\]\.size_quote"):
        parse_strategy_config(raw)
    raw["run"].pop("size", None)
    raw["run"]["size_quote"] = Decimal(400)
    universe = parse_strategy_config(raw).universe
    assert universe.discover and universe.top == 15 and universe.exclude == ("PUMPFUNUSDT",)
    assert not universe.idle and universe.quote == "USDT"
    passed = universe.with_instruments(("BTCUSDT", "ZECUSDT"))
    assert passed.instruments == ("BTCUSDT", "ZECUSDT") and not passed.discover


def test_any_venue_can_say_which_market_types_to_leave_out() -> None:
    markets = {
        "BTC/USDT:USDT": market("BTCUSDT", info={"kind": "crypto"}),
        "TSLA/USDT:USDT": market("TSLAUSDT", info={"kind": "Equity"}),
    }
    tickers = {s: ticker(5e8) for s in markets}
    venue = Venue(markets, tickers)
    # A venue with no default keeps both until the operator says what its field means.
    assert len(discover(venue, "othervenue", Rules(), clock=lambda: NOW_S).listed) == 2
    told = Rules(type_field="kind", exclude_types=("equity",))
    found = discover(venue, "othervenue", told, clock=lambda: NOW_S)
    assert [x.instrument for x in found.listed] == ["BTCUSDT"]
    assert found.dropped == {"not crypto": 1}
    # Turning crypto_only off on Bybit lets its stock perpetuals through.
    bybit = Venue(
        {"SOXL/USDT:USDT": market("SOXLUSDT", info={"symbolType": "ETF"})},
        {"SOXL/USDT:USDT": ticker(5e8)},
    )
    assert discover(bybit, "bybit", Rules(crypto_only=False), clock=lambda: NOW_S).listed
