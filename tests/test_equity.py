"""Stock perpetuals and tokenized stocks: the same order path, and the underlying exchange's hours.

The market shapes are the venues' own fields as ccxt 4.5.85 returned them on 2026-10-17: Bybit's
`symbolType` of `stock` and `xstocks`, OKX's `instCategory` of `3` on a contract.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from test_cycle import analyst_answer, cycle_config
from trading_research_agents.agents.cycle import run_cycle
from trading_research_agents.envelope import Envelope
from trading_research_agents.execution.session import SessionError, closes_at, is_open, refusal
from trading_research_agents.trading.market import Market

BYBIT_STOCK_PERP = {
    "symbol": "AAPL/USDT:USDT",
    "type": "swap",
    "swap": True,
    "contract": True,
    "linear": True,
    "contractSize": 1.0,
    "settle": "USDT",
    "info": {"symbolType": "stock", "contractType": "LinearPerpetual"},
}
BYBIT_XSTOCK = {
    "symbol": "AAPLX/USDT",
    "type": "spot",
    "spot": True,
    "quote": "USDT",
    "info": {"symbolType": "xstocks"},
}
OKX_STOCK_PERP = {**BYBIT_STOCK_PERP, "info": {"instCategory": "3"}}
OKX_SPOT = {**BYBIT_XSTOCK, "symbol": "XRP/USDT", "info": {"instCategory": "3"}}


def test_the_underlying_is_read_from_the_venues_own_fields() -> None:
    assert Market.from_ccxt("bybit", "AAPL/USDT:USDT", BYBIT_STOCK_PERP).asset_class == "equity"
    xstock = Market.from_ccxt("bybit", "AAPLX/USDT", BYBIT_XSTOCK)
    assert xstock.asset_class == "equity" and xstock.label == "spot"
    assert Market.from_ccxt("okx", "AAPL/USDT:USDT", OKX_STOCK_PERP).asset_class == "equity"
    # OKX's category is a contract field: on a spot pair it says nothing about the underlying.
    assert Market.from_ccxt("okx", "XRP/USDT", OKX_SPOT).asset_class == "crypto"
    # A venue that does not say is taken to list crypto; the operator names the exceptions.
    assert Market.from_ccxt("bitget", "TSLA/USDT:USDT", BYBIT_STOCK_PERP).asset_class == "crypto"
    # A stock perpetual is a linear perpetual: the order path already built for it applies.
    assert Market.from_ccxt("bybit", "AAPL/USDT:USDT", BYBIT_STOCK_PERP).label == "linear perpetual"


FRIDAY_NOON_NY = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)  # 12:00 in New York, daylight time
SATURDAY = datetime(2026, 10, 17, 16, 0, tzinfo=UTC)
WINTER_OPEN_NY = datetime(2026, 12, 7, 14, 30, tzinfo=UTC)  # 09:30 in New York, standard time


def test_the_regular_session_is_read_in_new_york_time() -> None:
    assert is_open(FRIDAY_NOON_NY) and not is_open(SATURDAY)
    assert is_open(WINTER_OPEN_NY) and not is_open(WINTER_OPEN_NY - timedelta(minutes=1))
    assert closes_at(FRIDAY_NOON_NY) == datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
    assert closes_at(WINTER_OPEN_NY) == datetime(2026, 12, 7, 21, 0, tzinfo=UTC)
    # A holiday the operator declares is a closed day.
    assert not is_open(FRIDAY_NOON_NY, (date(2026, 10, 16),))
    with pytest.raises(SessionError, match="aware"):
        is_open(datetime(2026, 10, 16, 12, 0))


def test_an_entry_waits_for_the_session_and_may_not_be_held_past_its_close() -> None:
    assert "exchange is closed" in refusal(SATURDAY, timedelta(minutes=15))
    assert refusal(FRIDAY_NOON_NY, timedelta(hours=3)) == ""
    late = refusal(FRIDAY_NOON_NY, timedelta(hours=5))
    assert "a hold of 300 minutes would run past the session's close at 16:00" in late


def _equity_cycle(tmp_path: Path, envelope: Envelope, when: datetime, **overrides: Any) -> Any:
    settings: dict[str, Any] = {
        "asset_class": "equity",
        "equity_session": "regular",
        "clock": lambda: when,
    }
    settings.update(overrides)
    cfg, fake, _ = cycle_config(tmp_path, envelope, [analyst_answer("long")], **settings)
    return run_cycle(cfg), fake


def test_a_cycle_on_an_equity_waits_while_its_exchange_is_closed(
    tmp_path: Path, envelope: Envelope
) -> None:
    result, fake = _equity_cycle(tmp_path, envelope, SATURDAY)
    assert result.held and result.intent is None and fake.calls == []
    assert any(
        r.startswith("session: the underlying's exchange is closed") for r in result.refusals
    )
    # Crypto keeps its hours: round the clock.
    crypto, _ = _equity_cycle(tmp_path, envelope, SATURDAY, asset_class="crypto")
    assert crypto.intent is not None


def test_a_cycle_on_an_equity_refuses_a_hold_past_the_close(
    tmp_path: Path, envelope: Envelope
) -> None:
    near_close = datetime(2026, 10, 16, 19, 50, tzinfo=UTC)  # 15:50 in New York
    result, _ = _equity_cycle(tmp_path, envelope, near_close)
    assert result.intent is None
    assert any("would run past the session's close" in r for r in result.refusals)
    open_long, _ = _equity_cycle(tmp_path, envelope, FRIDAY_NOON_NY)
    assert open_long.intent is not None


def test_the_operator_can_trade_an_equity_at_any_hour_deliberately(
    tmp_path: Path, envelope: Envelope
) -> None:
    result, _ = _equity_cycle(tmp_path, envelope, SATURDAY, equity_session="any")
    assert result.intent is not None


def test_the_equity_settings_are_read_and_checked() -> None:
    from trading_research_agents.trading.config import ConfigError, _mandate

    mandate = _mandate(
        {"execution": {"equity_closed": ["2026-11-26"], "equities": ["tslausdt"]}},
        "bitget/taker-taker",
    )
    assert mandate.equity_session == "regular" and mandate.equity_closed == ("2026-11-26",)
    assert mandate.equities == ("TSLAUSDT",)
    with pytest.raises(ConfigError, match='equity_session is "regular" or "any"'):
        _mandate({"execution": {"equity_session": "always"}}, "bybit/taker-taker")
    with pytest.raises(ConfigError, match="lists dates"):
        _mandate({"execution": {"equity_closed": ["Thanksgiving"]}}, "bybit/taker-taker")


def test_a_taker_price_is_held_inside_the_venues_published_band() -> None:
    from decimal import Decimal

    from trading_research_agents.trading.dispatch import VenueHand

    class Banded:
        has: dict[str, Any] = {}

        def market(self, symbol: str) -> dict[str, Any]:
            return {"id": "TSLA-USDT-SWAP", "symbol": symbol}

        def publicGetPublicPriceLimit(self, params: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
            assert params == {"instId": "TSLA-USDT-SWAP"}
            return {"data": [{"buyLmt": "199.34", "sellLmt": "195.42", "enabled": True}]}

    def hand(venue: str) -> VenueHand:
        return VenueHand(
            venue=venue,
            symbol="TSLA/USDT:USDT",
            max_notional=Decimal(1000),
            keys=("OKX_API_KEY", "OKX_API_SECRET"),
            client=Banded(),
        )

    touch = {"bid": "199.34", "ask": "199.46"}
    # 20 bps through the ask would be 199.86, past the band's 199.34: the band's edge goes out.
    assert hand("okx")._protected_price(touch, "buy") == Decimal("199.34")
    assert hand("okx")._protected_price({"bid": "195.50"}, "sell") == Decimal("195.42")
    # A venue that publishes no band keeps the slippage bound alone.
    assert hand("bybit")._protected_price(touch, "buy") == Decimal("199.85892")


def test_a_close_the_venue_accepts_but_does_not_fill_is_not_recorded_as_closed() -> None:
    from decimal import Decimal

    from trading_research_agents.trading.dispatch import VenueHand

    class Resting:
        has: dict[str, Any] = {}

        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def fetch_position(self, symbol: str) -> dict[str, Any]:
            return {"contracts": 5, "side": "short"}

        def market(self, symbol: str) -> dict[str, Any]:
            return {
                "symbol": symbol,
                "type": "swap",
                "swap": True,
                "linear": True,
                "contract": True,
                "contractSize": "0.01",
            }

        def create_order(self, *args: Any) -> dict[str, Any]:
            return {"id": "close-1", "status": "open", "filled": 0}

        def fetch_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            return {"id": order_id, "status": "open", "filled": 0}

        def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
            self.cancelled.append(order_id)
            return {"id": order_id}

    venue = Resting()
    hand = VenueHand(
        venue="okx",
        symbol="TSLA/USDT:USDT",
        max_notional=Decimal(1000),
        keys=("OKX_API_KEY", "OKX_API_SECRET"),
        client=venue,
        sleep=lambda s: None,
    )
    closed = hand.close_position("held past its limit")
    assert closed["closed"] is False and venue.cancelled == ["close-1"]
    assert "the close did not fill: the venue reports open" in closed["error"]
    assert "still holds -0.05" in closed["error"]
